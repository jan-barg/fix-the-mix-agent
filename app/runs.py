"""Run documents (DESIGN.md §4): one JSON file per run in storage, cached in memory.

Cloud Run runs with max-instances=1, so the in-memory copy is authoritative; every change is also
written to runs/<run_id>/run.json. Uploads arrive in parallel, so read-modify-write goes through a
per-run lock. Track IDs are fixed by slot ("user", "ref1"-"ref3"): replacing a file keeps its ID.
"""

import dataclasses
import tempfile
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.audio import decode, features, grid
from app.audio.features import TrackData
from app.config import ROOT, settings
from app.storage import get_storage, read_json, write_json

STATES = ["Draft", "Preparing", "NeedsClarification", "ReviewingSections", "Researching", "FollowingUp",
          "Writing", "Humanizing", "Validating", "Rendering", "Complete", "Partial", "Failed", "Interrupted"]
# States in which a /chat request is doing work. A run found in one with no request running (the
# server restarted mid-run) becomes Interrupted, and the next /chat message resumes it.
WORKING = {"Researching", "FollowingUp", "Writing", "Humanizing", "Validating", "Rendering"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class GridInfo(BaseModel):
    bpm: float
    first_beat_s: float
    downbeat_s: float  # first downbeat inside the track
    downbeat_confidence: str
    constant_tempo: bool


class TrackRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str  # "user", "ref1", "ref2", "ref3"
    role: str  # "user" or "reference"
    slot: int | None = None
    display_name: str  # "Your track", "Reference 1", ...
    filename: str
    comment: str = ""
    storage_path: str
    source: dict  # sha256, bytes, format, codec, sample_rate, channels, duration_s, decode_warnings
    prep: dict  # original_sample_peak_dbfs, applied_gain_db, silent, resampled_from
    grid: GridInfo


class SectionMap(BaseModel):
    version: int
    origin: str  # "proposed", "user", or "checked" (a demo map the producer checked, from demo/corpus.json)
    created_at: str = Field(default_factory=now)
    accepted: bool = False
    confirmed: bool = False
    bpm: float
    downbeat_s: float
    grid_source: str  # "detected" or "user"
    sections: list[dict]


class Run(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    created_at: str = Field(default_factory=now)
    state: str = "Draft"
    state_history: list[dict] = Field(default_factory=list)
    demo: bool = False
    comments: dict = Field(default_factory=lambda: {"global": ""})
    tracks: list[TrackRecord] = Field(default_factory=list)
    section_maps: dict[str, list[SectionMap]] = Field(default_factory=dict)
    brief: dict | None = None  # schemas.Brief, recorded by the Lead
    # rounds_used; results{task_id: researcher task and result}; finished (the Lead's finish_research);
    # stale: earlier research made obsolete by section edits
    research: dict = Field(default_factory=lambda: fresh_research())
    evidence_index: dict[str, dict] = Field(default_factory=dict)  # evidence_id -> tool, description, figures
    evidence_by_key: dict[str, str] = Field(default_factory=dict)  # cache key -> evidence_id
    usage: dict = Field(default_factory=lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    transcript: list[dict] = Field(default_factory=list)  # what the producer sees: {role, text, at}
    # status, html_path, pdf_path, pdf_error, checks, humanizer (app/report/pipeline.py)
    report: dict | None = None

    def track(self, track_id: str) -> TrackRecord | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    def current_map(self, track_id: str) -> SectionMap | None:
        maps = self.section_maps.get(track_id)
        return maps[-1] if maps else None

    def ready_for_sections(self) -> bool:
        return self.track("user") is not None and any(t.role == "reference" for t in self.tracks)

    def sections_confirmed(self) -> bool:
        return self.ready_for_sections() and all(
            (m := self.current_map(t.id)) is not None and m.confirmed for t in self.tracks)


class RunNotFound(Exception):
    pass


_runs: dict[str, Run] = {}
_locks: dict[str, threading.Lock] = {}
active: set[str] = set()  # runs with a /chat request in progress in this process
_locks_guard = threading.Lock()
_load_guard = threading.Lock()  # one cold load at a time, so a slow loader cannot replace a newer copy


def _path(run_id: str) -> str:
    return f"runs/{run_id}/run.json"


def lock(run_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(run_id, threading.Lock())


def create(demo: bool = False) -> Run:
    run = Run(id=str(uuid.uuid4()), demo=demo)
    run.state_history.append({"state": run.state, "at": run.created_at})
    save(run)
    return run


def get(run_id: str) -> Run:
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise RunNotFound(run_id) from None
    if run_id in _runs:
        return _runs[run_id]
    with _load_guard:
        if run_id in _runs:  # loaded (and possibly already edited) while this request waited
            return _runs[run_id]
        storage = get_storage()
        if not storage.exists(_path(run_id)):
            raise RunNotFound(run_id)
        run = Run.model_validate(read_json(_path(run_id)))
        if run.state in WORKING and run_id not in active:
            set_state(run, "Interrupted", run.state)  # detail: the state to resume
            write_json(_path(run_id), run.model_dump(mode="json"))
        _runs[run_id] = run
        return run


def save(run: Run) -> None:
    _runs[run.id] = run
    write_json(_path(run.id), run.model_dump(mode="json"))


def fresh_research() -> dict:
    return {"rounds_used": 0, "results": {}, "finished": None, "stale": []}


def archive_research(run: Run) -> bool:
    """Move research findings to research.stale, because the tracks or sections they were measured on
    changed (SPEC §2.3). Measurements stay cached by what they depend on. Returns whether there was any."""
    r = run.research
    if not (r.get("results") or r.get("finished")):
        return False
    old = {k: v for k, v in r.items() if k != "stale"}
    run.research = {**fresh_research(), "stale": [*r.get("stale", []), old]}
    run.report = None  # written from those findings
    return True


def add_usage(run_id: str, input_tokens: int, output_tokens: int, cost_usd: float | None) -> None:
    """Add one model call to the run's totals."""
    with lock(run_id):
        run = get(run_id)
        run.usage["calls"] += 1
        run.usage["input_tokens"] += input_tokens
        run.usage["output_tokens"] += output_tokens
        run.usage["cost_usd"] = round(run.usage["cost_usd"] + (cost_usd or 0.0), 4)
        save(run)


def add_message(run_id: str, role: str, text: str) -> None:
    with lock(run_id):
        run = get(run_id)
        run.transcript.append({"role": role, "text": text, "at": now()})
        save(run)


def load_lead(run_id: str) -> list[dict]:
    """The Lead's append-only conversation, kept apart from run.json so the UI payload stays small."""
    path = f"runs/{run_id}/lead.json"
    storage = get_storage()
    return read_json(path) if storage.exists(path) else []


def save_lead(run_id: str, messages: list[dict]) -> None:
    write_json(f"runs/{run_id}/lead.json", messages)


def set_state(run: Run, state: str, detail: str = "") -> None:
    assert state in STATES, state
    if run.state != state:
        run.state = state
        run.state_history.append({"state": state, "at": now(), **({"detail": detail} if detail else {})})


# --- Track audio and analysis ---


def read_source(storage_path: str) -> bytes:
    """Original file bytes. Demo tracks live in demo/audio/: in the repo checkout locally, in the bucket on Cloud Run."""
    local = ROOT / storage_path
    if storage_path.startswith("demo/") and local.is_file():
        return local.read_bytes()
    return get_storage().read(storage_path)


# Decoding and base analysis are CPU- and memory-heavy (a 10-minute stereo file decodes to about
# 200 MB of float32 before analysis copies). At most this many run at once, however many uploads,
# demo runs and cache misses arrive together.
analysis_slots = threading.BoundedSemaphore(2)


class _AudioCache:
    """Decoded, normalised audio and base features by file hash (LRU). Re-decoding costs about a second.

    Concurrent misses for the same file wait for one decode instead of each decoding it.
    """

    def __init__(self, size: int = 6):
        self.size, self.items, self.guard = size, OrderedDict(), threading.Lock()
        self.key_locks: dict[str, threading.Lock] = {}

    def lookup(self, sha256: str) -> tuple[decode.PreparedAudio, features.BaseFeatures] | None:
        with self.guard:
            if sha256 in self.items:
                self.items.move_to_end(sha256)
                return self.items[sha256]
        return None

    def key_lock(self, sha256: str) -> threading.Lock:
        with self.guard:
            return self.key_locks.setdefault(sha256, threading.Lock())

    def get(self, sha256: str, storage_path: str, suffix: str) -> tuple[decode.PreparedAudio, features.BaseFeatures]:
        if (hit := self.lookup(sha256)) is not None:
            return hit
        with self.key_lock(sha256):
            if (hit := self.lookup(sha256)) is not None:
                return hit
            data = read_source(storage_path)
            with analysis_slots:
                audio = prepare_bytes(data, suffix)
                feats = features.get_or_compute(audio, sha256, get_storage())
            self.put(sha256, audio, feats)
            return audio, feats

    def put(self, sha256: str, audio: decode.PreparedAudio, feats: features.BaseFeatures) -> None:
        with self.guard:
            self.items[sha256] = (audio, feats)
            self.items.move_to_end(sha256)
            while len(self.items) > self.size:
                self.items.popitem(last=False)


audio_cache = _AudioCache()


def prepare_bytes(data: bytes, suffix: str) -> decode.PreparedAudio:
    """Validate, decode and normalise uploaded bytes (FFmpeg needs a file; /tmp is memory on Cloud Run)."""
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(data)
        f.flush()
        return decode.prepare(Path(f.name), settings.analysis_sample_rate, settings.min_duration_s, settings.max_duration_s)


def track_data(run: Run, track_id: str) -> TrackData:
    """A track ready for measurement, on the grid of its current section map: a BPM or downbeat the
    user corrected in the editor replaces the detected grid (a copy; the cached features are shared)."""
    t = run.track(track_id)
    if t is None:
        raise KeyError(track_id)
    audio, feats = audio_cache.get(t.source["sha256"], t.storage_path, Path(t.filename).suffix)
    current = run.current_map(track_id)
    if current is not None and current.grid_source == "user":
        feats = dataclasses.replace(feats, grid=grid.user_grid(current.bpm, current.downbeat_s, audio.duration_s))
    return TrackData(t.id, t.display_name, t.role, audio, feats)


def grid_info(feats: features.BaseFeatures) -> GridInfo:
    g = feats.grid
    downbeats = g.downbeats_s
    inside = downbeats[downbeats >= -grid.START_TOLERANCE_S]
    downbeat = float(inside[0]) if inside.size else float(g.beats_s[0])
    return GridInfo(bpm=g.bpm, first_beat_s=float(g.beats_s[0]), downbeat_s=downbeat,
                    downbeat_confidence=g.downbeat_confidence, constant_tempo=g.constant_tempo)
