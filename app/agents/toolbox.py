"""Measurement tools as the agents call them (DESIGN.md §5.4, §7.2): scope references in, Evidence out.

Every measurement runs through here, whether the Lead asked for it or a researcher's default bundle
needs it. Results are cached per run by what they depend on (files, ranges, grid, parameters, config),
stored under runs/<run_id>/evidence/ and figures/, and logged as trace events.
"""

import json
import threading
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from app import runs, tools, trace
from app.agents import context, schemas
from app.audio import kick
from app.audio.features import Scope, TrackData, whole_track
from app.evidence import Evidence, Figure, cache_key
from app.runs import Run
from app.storage import get_storage

# matplotlib's pyplot keeps global state, so helpers run one at a time. Model calls still overlap.
HELPER_LOCK = threading.Lock()

MEASUREMENTS: dict[str, type[BaseModel]] = {
    "measure_loudness": schemas.Scopes,
    "measure_tonal_balance": schemas.TonalArgs,
    "measure_stereo": schemas.Scopes,
    "find_kick_candidates": schemas.KickCandidateArgs,
    "analyse_kick": schemas.AnalyseKickArgs,
    "measure_low_end": schemas.LowEndArgs,
    "render_spectrogram": schemas.SpectrogramArgs,
}

_memory: dict[str, dict[str, Evidence]] = {}  # run_id -> evidence_id -> Evidence (with figure PNGs)
_memory_guard = threading.Lock()


class ToolError(Exception):
    """A request the tool cannot run (bad scope, bad arguments). The message goes back to the model."""


def _store(run: Run, ev: Evidence, key: str) -> None:
    storage = get_storage()
    for fig in ev.figures:
        fig.path = f"runs/{run.id}/figures/{fig.id}.png"
        storage.write(fig.path, fig.png, content_type="image/png")
    storage.write(f"runs/{run.id}/evidence/{ev.id}.json", json.dumps(ev.to_dict(), default=str).encode(),
                  content_type="application/json")
    with _memory_guard:
        _memory.setdefault(run.id, {})[ev.id] = ev
    with runs.lock(run.id):
        live = runs.get(run.id)
        live.evidence_index[ev.id] = {"tool": ev.tool, "description": ev.source["description"],
                                      "figures": [{"id": f.id, "title": f.title} for f in ev.figures],
                                      "status": ev.reliability["status"]}
        live.evidence_by_key[key] = ev.id
        runs.save(live)


def load(run_id: str, evidence_id: str) -> Evidence:
    with _memory_guard:
        if evidence_id in _memory.get(run_id, {}):
            return _memory[run_id][evidence_id]
    storage = get_storage()
    path = f"runs/{run_id}/evidence/{evidence_id}.json"
    if not storage.exists(path):
        raise ToolError(f"There is no evidence {evidence_id!r} in this run.")
    d = json.loads(storage.read(path))
    figures = [Figure(id=f["id"], title=f["title"], caption=f["caption"], png=storage.read(f["path"]), path=f["path"])
               for f in d.pop("figures")]
    ev = Evidence(**d, figures=figures)
    with _memory_guard:
        _memory.setdefault(run_id, {})[evidence_id] = ev
    return ev


class Toolbox:
    """Runs measurements for one run. Synchronous: call it from a worker thread."""

    def __init__(self, run: Run):
        self.run = run
        self._tracks: dict[str, TrackData] | None = None

    def tracks(self) -> dict[str, TrackData]:
        if self._tracks is None:
            self._tracks = {t.id: runs.track_data(self.run, t.id) for t in self.run.tracks}
        return self._tracks

    def scopes(self, refs: list[str]) -> list[Scope]:
        if not refs:
            raise ToolError("Name at least one scope.")
        try:
            return [context.resolve(self.run, ref, self.tracks()) for ref in refs]
        except context.ScopeError as e:
            raise ToolError(str(e)) from None

    def measure(self, name: str, raw_args: dict, *, agent: str, parent_task_id: str | None) -> Evidence:
        """Run (or reuse) one measurement and record it in the trace as a helper run."""
        if name not in MEASUREMENTS:
            raise ToolError(f"Unknown measurement {name!r}.")
        try:
            args = MEASUREMENTS[name].model_validate(raw_args)
        except ValidationError as e:
            raise ToolError(f"Invalid arguments for {name}: {e.errors()[0]['msg']} at {e.errors()[0]['loc']}") from None
        compute, key = self._plan(name, args)
        event = trace.start(self.run.id, name=name, agent=agent, kind="helper", args=raw_args, parent_task_id=parent_task_id)
        try:
            # The cache is checked under the lock, so two researchers asking for the same measurement
            # at once compute it once.
            with HELPER_LOCK:
                cached = runs.get(self.run.id).evidence_by_key.get(key)
                if cached:
                    ev = load(self.run.id, cached)
                    trace.end(event, f"reused {ev.id}: {ev.source['description']}", evidence_id=ev.id, cached=True)
                    return ev
                ev = compute()
                _store(self.run, ev, key)
            trace.end(event, f"{ev.id}: {ev.source['description']} ({ev.reliability['status']}, {len(ev.figures)} figures)",
                      evidence_id=ev.id)
            return ev
        except Exception as e:
            trace.end(event, f"{type(e).__name__}: {e}", status="error")
            raise

    def _plan(self, name: str, a: Any) -> tuple[Callable[[], Evidence], str]:
        """The computation and the cache key that identifies its result."""
        if name == "measure_loudness":
            scopes = self.scopes(a.scopes)
            return (lambda: tools.measure_loudness(scopes)), cache_key(name, scopes, {})
        if name == "measure_stereo":
            scopes = self.scopes(a.scopes)
            return (lambda: tools.measure_stereo(scopes)), cache_key(name, scopes, {})
        if name == "measure_tonal_balance":
            scopes = self.scopes(a.scopes)
            missing = [c for c in a.combine if c not in a.scopes]
            if missing:
                raise ToolError(f"combine names scopes that are not in scopes: {missing}")
            combine = [a.scopes.index(c) for c in a.combine] or None
            params = {"mode": a.mode, "combine": combine}
            return (lambda: tools.measure_tonal_balance(scopes, a.mode, combine)), cache_key(name, scopes, params)
        if name == "measure_low_end":
            scopes = self.scopes(a.scopes)
            bands = list(a.bands) or None
            return (lambda: tools.measure_low_end(scopes, bands)), cache_key(name, scopes, {"bands": bands})
        if name == "render_spectrogram":
            scopes = self.scopes(a.scopes)
            params = {"preset": a.preset, "fmin": a.fmin, "fmax": a.fmax}
            return (lambda: tools.render_spectrogram(scopes, a.preset, a.fmin, a.fmax)), cache_key(name, scopes, params)
        if name == "find_kick_candidates":
            track = self._track(a.track)
            ranges = [(s.start_s, s.end_s) for s in self.scopes(a.within)] if a.within else None
            n = min(max(a.n, 1), 5)
            return (lambda: tools.find_kick_candidates(track, ranges, n)), \
                cache_key(name, [whole_track(track)], {"ranges": ranges, "n": n})
        if name == "analyse_kick":
            if not a.kicks:
                raise ToolError("Name at least one kick.")
            picks = []
            for k in a.kicks:
                track = self._track(k.track)
                if not 0 <= k.time_s < track.audio.duration_s - kick.CROP_S:
                    raise ToolError(f"{k.track} has no full 400 ms kick crop at {k.time_s:.2f} s.")
                picks.append((track, k.time_s, kick.next_onset(track, k.time_s)))
            scopes = [Scope(t, s - kick.CROP_PRE_S, s + kick.CROP_S, f"kick at {s:.2f} s", kind="event") for t, s, _ in picks]
            return (lambda: tools.analyse_kicks(picks)), cache_key(name, scopes, {"next": [n for _, _, n in picks]})
        raise ToolError(f"Unknown measurement {name!r}.")

    def _track(self, track_id: str) -> TrackData:
        tracks = self.tracks()
        if track_id not in tracks:
            raise ToolError(f"There is no track {track_id!r}. Tracks: {', '.join(tracks)}.")
        return tracks[track_id]


def lead_summary(ev: Evidence) -> str:
    """What the Lead reads back from a measurement: the evidence summary (scalars, method, reliability)."""
    return ev.summary_text()
