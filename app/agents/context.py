"""What the agents can name: sections, matched section pairs and scope references (DESIGN.md §5.4, §7.2).

The Lead and researchers never see section IDs or seconds-only ranges. A scope reference is one of:

- "user"               a whole track
- "ref1/drop 2"        a confirmed section, named by label and, when the label repeats, its order
- "user@60.5-90.0"     any passage, in seconds

Sections are matched across tracks by label and order: your "drop 1" pairs with each reference's
"drop 1". "unknown" sections are never matched.
"""

import re
from dataclasses import dataclass

from app.audio.features import Scope, TrackData, clock, whole_track
from app.runs import Run


class ScopeError(ValueError):
    """A scope reference that does not name anything in this run. The message goes back to the model."""


@dataclass(frozen=True)
class NamedSection:
    track_id: str
    name: str  # "drop 1", "breakdown"
    label: str
    section_id: str
    start_s: float
    end_s: float
    start_bar: int
    end_bar: int

    @property
    def ref(self) -> str:
        return f"{self.track_id}/{self.name}"

    def describe(self) -> str:
        bars = self.end_bar - self.start_bar
        return f"{self.ref}: {clock(self.start_s)}–{clock(self.end_s)}, bars {self.start_bar}–{self.end_bar - 1} ({bars} bars)"


def named_sections(run: Run, track_id: str) -> list[NamedSection]:
    current = run.current_map(track_id)
    if current is None:
        return []
    counts: dict[str, int] = {}
    for s in current.sections:
        counts[s["label"]] = counts.get(s["label"], 0) + 1
    seen: dict[str, int] = {}
    out = []
    for s in current.sections:
        label = s["label"]
        seen[label] = seen.get(label, 0) + 1
        name = f"{label} {seen[label]}" if counts[label] > 1 else label
        out.append(NamedSection(track_id, name, label, s["id"], s["start_s"], s["end_s"], s["start_bar"], s["end_bar"]))
    return out


def matched_pairs(run: Run) -> list[dict]:
    """Your sections paired with each reference's section of the same label and order."""
    refs = [t.id for t in run.tracks if t.role == "reference"]
    by_track = {tid: named_sections(run, tid) for tid in ["user", *refs]}

    def order_of(sections: list[NamedSection]) -> dict[tuple[str, int], NamedSection]:
        seen: dict[str, int] = {}
        out = {}
        for s in sections:
            seen[s.label] = seen.get(s.label, 0) + 1
            out[(s.label, seen[s.label])] = s
        return out

    user = order_of(by_track["user"])
    pairs = []
    for key, s in user.items():
        if s.label == "unknown":
            continue
        matches = {tid: order_of(by_track[tid]).get(key) for tid in refs}
        pairs.append({"user": s.ref, **{tid: m.ref for tid, m in matches.items() if m is not None}})
    return pairs


def default_focus(run: Run) -> list[str]:
    """The matched pair a specialist looks at when the Lead names none: the first section the brief
    focuses on ("drop" means your first drop), else the first drop, else the first matched pair."""
    pairs = matched_pairs(run)
    wanted = [f.strip().lower() for f in (run.brief or {}).get("focus_sections") or []]
    for name in [*wanted, "drop"]:
        pair = next((p for p in pairs if p["user"].split("/", 1)[1] in (name, f"{name} 1")), None)
        if pair:
            return list(pair.values())
    return list(pairs[0].values()) if pairs else []


_PASSAGE = re.compile(r"^(?P<track>[a-z0-9]+)@(?P<start>\d+(\.\d+)?)-(?P<end>\d+(\.\d+)?)$")


def resolve(run: Run, ref: str, tracks: dict[str, TrackData]) -> Scope:
    """Turn a scope reference into a Scope on the track's current grid. `tracks` holds loaded TrackData."""
    ref = ref.strip()
    if m := _PASSAGE.match(ref):
        track = _track(tracks, m["track"])
        start, end = float(m["start"]), float(m["end"])
        if not 0 <= start < end <= track.audio.duration_s + 0.05:
            raise ScopeError(f"{ref}: the passage must lie inside the track (0–{track.audio.duration_s:.1f} s).")
        if end - start < 1.0:
            raise ScopeError(f"{ref}: a passage must be at least 1 s long.")
        return Scope(track, start, min(end, track.audio.duration_s), "passage")
    track_id, _, name = ref.partition("/")
    track = _track(tracks, track_id)
    if not name:
        return whole_track(track)
    section = next((s for s in named_sections(run, track_id) if s.name == name.strip()), None)
    if section is None:
        names = ", ".join(s.name for s in named_sections(run, track_id))
        raise ScopeError(f"{ref}: {track_id} has no section called {name!r}. Its sections: {names}.")
    return Scope(track, section.start_s, section.end_s, section.name, section_id=section.section_id)


def _track(tracks: dict[str, TrackData], track_id: str) -> TrackData:
    if track_id not in tracks:
        raise ScopeError(f"There is no track {track_id!r}. Tracks: {', '.join(tracks)}.")
    return tracks[track_id]


def track_of(ref: str) -> str:
    return re.split(r"[/@]", ref.strip(), maxsplit=1)[0]
