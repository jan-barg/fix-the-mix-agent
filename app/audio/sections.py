"""Section proposal (SPEC §5.4, DESIGN.md §5.3): a starting point the user corrects in the editor.

Bars come from the grid. Each bar gets a few features (loudness, low-end share, kick presence,
brightness, width), z-scored per track. Boundaries are allowed only on phrase starts (every 8 bars,
phrase offset chosen by the data) where consecutive 4-bar windows differ most. Labels come from
energy and kick presence and are suggestions: each carries "likely" or "guess", and anything that
does not fit a rule stays "unknown". Chorus/verse/bridge are only ever chosen by the user.
"""

from dataclasses import asdict, dataclass

import numpy as np

from app.audio import bands, grid
from app.audio.features import TrackData

LABELS = ["intro", "build", "drop", "breakdown", "verse", "chorus", "bridge", "outro", "unknown"]
PHRASE_BARS = 8
WINDOW_BARS = 4
DROP_WITHIN_LU = 3.0  # a drop is within this of the loudest section
QUIET_BELOW_LU = 6.0  # more than this below the loudest section counts as low energy
KICK_PRESENT_DB = 3.0  # beat-synchronous low-end modulation (see _kick_modulation)
KICK_ABSENT_DB = 1.5


@dataclass
class Section:
    id: str
    label: str
    start_s: float
    end_s: float
    start_bar: int  # 1 = the bar starting at the first downbeat; 0 = a pickup before it
    end_bar: int  # exclusive
    label_confidence: str | None = None  # "likely" or "guess" for proposals; None once the user edits

    def to_dict(self) -> dict:
        return asdict(self)


def bar_starts(track: TrackData) -> np.ndarray:
    """Downbeat times inside the track (bar 1 first)."""
    downbeats = track.features.grid.downbeats_s
    return downbeats[(downbeats >= -grid.START_TOLERANCE_S) & (downbeats < track.audio.duration_s)]


def bar_index(time_s: float, downbeats: np.ndarray, bar_s: float) -> int:
    """Bar number of a time: 1 at the first downbeat, counted on the straight grid."""
    return bar_span(time_s, time_s, downbeats[0], bar_s)[0]


def bar_span(start_s: float, end_s: float, downbeat_s: float, bar_s: float) -> tuple[int, int]:
    """(first bar, end bar exclusive) of a time range; times within 2% of a bar line count as on it,
    so rounding to milliseconds never shifts a boundary into the neighbouring bar. A range from the
    file start is bar 1 when the first downbeat is within grid.START_TOLERANCE_S of it (no pickup)."""
    def position(t: float) -> float:
        x = (t - downbeat_s) / bar_s
        return float(round(x)) if abs(x - round(x)) < 0.02 else x

    first = max(0, int(np.floor(position(start_s))) + 1)
    if start_s <= 0 and downbeat_s <= grid.START_TOLERANCE_S:
        first = max(first, 1)
    return first, int(np.ceil(position(end_s))) + 1


def _power_mean_db(times: np.ndarray, level_db: np.ndarray, lo: float, hi: float) -> float:
    mask = (times >= lo) & (times < hi) & np.isfinite(level_db)
    return float(bands.db(np.mean(10 ** (level_db[mask] / 10)))) if mask.any() else -70.0


def bar_features(track: TrackData, starts: np.ndarray) -> dict[str, np.ndarray]:
    f = track.features
    ends = np.append(starts[1:], track.audio.duration_s)
    L, sp = f.loudness, f.spectra
    centres = sp.bands.centres
    power = sp.channel_average
    beats = f.grid.beats_s
    kick = _kick_modulation(track)

    out = {k: np.zeros(len(starts)) for k in ("loudness", "low_share", "kick", "brightness", "width")}
    for i, (a, b) in enumerate(zip(starts, ends)):
        out["loudness"][i] = _power_mean_db(L.times_s, L.momentary_lufs, a, b)
        frames = sp.frames_in(a, b)
        p = power[:, frames].sum(axis=1) if frames.any() else np.zeros(len(centres))
        total = p.sum() + bands.EPS
        out["low_share"][i] = bands.db(p[centres < 120].sum() / total)
        out["brightness"][i] = float(np.sum(np.log2(centres) * p) / total)
        if sp.side is not None and frames.any():
            out["width"][i] = bands.db(sp.side[:, frames].sum() / (sp.mid[:, frames].sum() + bands.EPS))
        in_bar = (beats >= a) & (beats < b)
        out["kick"][i] = float(np.median(kick[in_bar])) if in_bar.any() else 0.0
    return out


def _kick_modulation(track: TrackData) -> np.ndarray:
    """Per beat: 30-120 Hz level in the 60 ms after the beat minus its level from 60% to 90% of the beat.

    About 0 dB without a kick, 4-8 dB for a kick over a bass line, 40 dB or more for an exposed kick.
    Onset strength cannot tell these apart: in a dense drop the bass masks the kick's onset.
    Clipped so exposed kicks do not dominate the z-scored novelty.
    """
    env = track.features.envelopes
    times = env["sub"].times_s
    power = 10 ** (env["sub"].level_db / 10) + 10 ** (env["low"].level_db / 10)
    cumulative = np.concatenate([[0.0], np.cumsum(power, dtype=np.float64)])
    beat = track.features.grid.beat_s

    def mean_db(lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        i, j = np.searchsorted(times, lo), np.searchsorted(times, hi)
        return bands.db((cumulative[j] - cumulative[i]) / np.maximum(j - i, 1))

    beats = track.features.grid.beats_s
    return np.clip(mean_db(beats, beats + 0.06) - mean_db(beats + 0.6 * beat, beats + 0.9 * beat), -10.0, 20.0)


def _zscore(v: np.ndarray) -> np.ndarray:
    return (v - v.mean()) / (v.std() + 1e-9)


def _novelty(features: dict[str, np.ndarray]) -> np.ndarray:
    """Novelty at each bar start: distance between the 4 bars before and the 4 bars after."""
    x = np.stack([_zscore(v) for v in features.values()], axis=1)
    n = len(x)
    nov = np.zeros(n)
    for b in range(1, n):
        before, after = x[max(0, b - WINDOW_BARS):b], x[b:b + WINDOW_BARS]
        nov[b] = np.linalg.norm(before.mean(axis=0) - after.mean(axis=0))
    return nov


def propose(track: TrackData) -> list[Section]:
    starts = bar_starts(track)
    duration = track.audio.duration_s
    bar_s = 4 * track.features.grid.beat_s
    if len(starts) < 2 * PHRASE_BARS:
        return [Section("s1", "unknown", 0.0, duration, 0, bar_index(duration, starts, bar_s) if len(starts) else 0, "guess")]

    feats = bar_features(track, starts)
    nov = _novelty(feats)
    # Phrase offset: the 8-bar alignment whose bar starts carry the most novelty (tracks often open
    # with a pickup or an odd-length intro).
    offset = int(np.argmax([nov[k::PHRASE_BARS].sum() for k in range(PHRASE_BARS)]))
    candidates = np.arange(offset if offset else PHRASE_BARS, len(starts), PHRASE_BARS)
    candidates = candidates[(candidates >= WINDOW_BARS) & (candidates <= len(starts) - WINDOW_BARS)]
    if candidates.size:
        z = _zscore(nov[candidates]) if candidates.size > 1 else np.ones(1)
        chosen = candidates[z > 0]
    else:
        chosen = candidates
    edges_bar = [0, *chosen.tolist(), len(starts)]
    edges_s = [0.0, *starts[chosen].tolist(), duration]

    # Per-section energy and kick presence for labelling.
    sections = []
    for i in range(len(edges_bar) - 1):
        a, b = edges_bar[i], edges_bar[i + 1]
        loud = _power_mean_db(track.features.loudness.times_s, track.features.loudness.momentary_lufs, edges_s[i], edges_s[i + 1])
        half = (edges_s[i] + edges_s[i + 1]) / 2
        rise = (_power_mean_db(track.features.loudness.times_s, track.features.loudness.momentary_lufs, half, edges_s[i + 1])
                - _power_mean_db(track.features.loudness.times_s, track.features.loudness.momentary_lufs, edges_s[i], half))
        kick = float(np.mean(feats["kick"][a:b])) if b > a else 0.0
        sections.append({"start_s": edges_s[i], "end_s": edges_s[i + 1], "start_bar": a + 1 if i else 0,
                         "end_bar": b + 1, "loud": loud, "rise": rise, "kick": kick})
    if starts[0] <= grid.START_TOLERANCE_S:  # no pickup: the first section starts on bar 1
        sections[0]["start_bar"] = 1

    _label(sections)
    merged = _merge_same_labels(sections)
    return [Section(f"s{i + 1}", s["label"], round(s["start_s"], 3), round(s["end_s"], 3), s["start_bar"], s["end_bar"],
                    s["confidence"]) for i, s in enumerate(merged)]


def _label(sections: list[dict]) -> None:
    loudest = max(s["loud"] for s in sections)
    for s in sections:
        below = loudest - s["loud"]
        if below <= DROP_WITHIN_LU and s["kick"] >= KICK_PRESENT_DB:
            s["label"] = "drop"
            s["confidence"] = "likely" if below <= DROP_WITHIN_LU / 2 and s["kick"] >= 2 * KICK_PRESENT_DB else "guess"
        elif s["kick"] < KICK_ABSENT_DB or below > QUIET_BELOW_LU:
            s["label"] = "breakdown"
            s["confidence"] = "likely" if s["kick"] < KICK_ABSENT_DB / 2 else "guess"
        else:
            s["label"], s["confidence"] = "unknown", "guess"
    for i, s in enumerate(sections[:-1]):
        if s["label"] != "drop" and sections[i + 1]["label"] == "drop" and (s["rise"] > 1.0 or s["kick"] < KICK_PRESENT_DB):
            s["label"] = "build"
            s["confidence"] = "likely" if s["rise"] > 3.0 else "guess"
    if sections[0]["label"] != "drop":
        sections[0]["label"], sections[0]["confidence"] = "intro", "likely"
    if len(sections) > 1 and sections[-1]["label"] != "drop":
        sections[-1]["label"], sections[-1]["confidence"] = "outro", "likely"


def _merge_same_labels(sections: list[dict]) -> list[dict]:
    merged = [dict(sections[0])]
    for s in sections[1:]:
        last = merged[-1]
        if s["label"] == last["label"] and s["label"] != "build":
            last["end_s"], last["end_bar"] = s["end_s"], s["end_bar"]
            if s["confidence"] == "guess":
                last["confidence"] = "guess"
        else:
            merged.append(dict(s))
    return merged
