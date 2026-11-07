"""Section proposal (SPEC §5.4, DESIGN.md §5.3): a starting point the user corrects in the editor.

Drops first. A drop starts where the full low end comes back after a build or breakdown thinned it out;
builds are the bars before a drop while the low end thins; breakdowns have no low end pumping on the beat
and few hat or snare spikes. What is left is split where the music changes most (on a 4-bar grid): the
first piece is the intro, anything after the last drop the outro, the rest verses. Each label carries
"likely" or "guess". Tuned on 8 tracks the producer sectioned by hand (DESIGN.md §5.3).
"""

import warnings
from dataclasses import asdict, dataclass

import numpy as np

from app.audio import bands, grid
from app.audio.features import TrackData, mid_signal

LABELS = ["intro", "build", "drop", "breakdown", "verse", "chorus", "bridge", "outro", "unknown"]
MIN_BARS = 16  # shorter tracks get one "unknown" section
PHRASE = 4  # boundaries fall on 4-bar lines (offset chosen by the data)
HAT_BAND = (6000.0, 16000.0)  # hats, shakers, snare and clap tops
PHASE_BINS = 24  # points per beat in the beat-folded envelopes
# Drops: the full low end (30-120 Hz within FULL_DB of the track's loudest, pumping at least PULSE_MIN_DB within
# the beat) coming back at least COMEBACK_DB above the previous COMEBACK_BARS bars, no earlier than MIN_DROP_BAR.
FULL_DB, PULSE_MIN_DB, COMEBACK_DB, COMEBACK_BARS, MIN_DROP_BAR = 4.5, 4.0, 9.5, 2, 20
DROP_MAX_BARS, DROP_END_DB, DROP_GAP_BARS = 16, 7.0, 3  # a drop lasts while its low end stays within DROP_END_DB
# Builds: before a drop, the low end falls by BUILD_SLOPE_DB a bar or ends BUILD_DB below full.
BUILD_SLOPE_DB, BUILD_DB = 0.8, 10.0
# Breakdowns: low end pumping under BREAKDOWN_PULSE_DB and hats under BREAKDOWN_HAT_DB, for at least BREAKDOWN_BARS.
BREAKDOWN_PULSE_DB, BREAKDOWN_HAT_DB, BREAKDOWN_LOW_DB, BREAKDOWN_BARS = 9.0, 7.0, 30.0, 8
# Splitting the rest: change between the 2 bars either side of each bar, from these features (z-scored per
# track) plus the shape of the 200 Hz-5 kHz spectrum (vocals, leads); kept where it stands CHANGE_Z above the rest.
CHANGE_WEIGHTS = {"loud": 0.3, "low": 2.8, "hat": 1.3, "hat_pulse": 2.4, "width": 1.6, "low_share": 0.8, "upper": 2.4}
SHAPE_WEIGHT, CHANGE_BARS, CHANGE_Z = 1.5, 2, 0.9


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


def _level_db(times: np.ndarray, level_db: np.ndarray, a: float, b: float) -> float:
    m = (times >= a) & (times < b)
    return float(bands.db(np.mean(10 ** (level_db[m] / 10)))) if m.any() else -90.0


def _pulse_db(times: np.ndarray, level_db: np.ndarray, beats: np.ndarray, beat_s: float) -> float:
    """How far a level swings within the beat: the median envelope through the beat (over the given beats), 95th
    minus 5th percentile. The same wherever in the beat the peak sits (a kick on the beat, a bass on the offbeat, a
    grid that drifts a little), which a fixed window after the beat is not."""
    if not len(beats):
        return 0.0
    pts = beats[:, None] + np.arange(PHASE_BINS)[None, :] / PHASE_BINS * beat_s
    vals = np.interp(pts, times, level_db, left=np.nan, right=np.nan)
    with warnings.catch_warnings():  # a beat running off the end of the file leaves all-NaN points
        warnings.simplefilter("ignore", RuntimeWarning)
        profile = np.nanmedian(vals, axis=0)
    profile = profile[np.isfinite(profile)]
    return float(np.percentile(profile, 95) - np.percentile(profile, 5)) if profile.size > 4 else 0.0


def bar_features(track: TrackData, starts: np.ndarray) -> dict[str, np.ndarray]:
    f = track.features
    ends = np.append(starts[1:], track.audio.duration_s)
    beats, beat_s = f.grid.beats_s, f.grid.beat_s
    env = f.envelopes
    low_t = env["sub"].times_s
    low_db = bands.db(10 ** (env["sub"].level_db / 10) + 10 ** (env["low"].level_db / 10))
    hat = bands.band_envelope(mid_signal(track.audio.signal), track.audio.sample_rate, HAT_BAND, smooth_s=0.01)
    L, sp = f.loudness, f.spectra
    centres = sp.bands.centres
    power = sp.channel_average
    shape_band = (centres >= 200) & (centres <= 5000)
    keys = ("loud", "low", "upper", "hat", "pulse", "hat_pulse", "low_share", "width")
    out = {k: np.zeros(len(starts)) for k in keys}
    out["shape"] = np.zeros((len(starts), int(shape_band.sum())))
    for i, (a, b) in enumerate(zip(starts, ends)):
        out["loud"][i] = _power_mean_db(L.times_s, L.momentary_lufs, a, b)
        out["low"][i] = _level_db(low_t, low_db, a, b)
        out["upper"][i] = _level_db(env["upper_bass"].times_s, env["upper_bass"].level_db, a, b)
        out["hat"][i] = _level_db(hat.times_s, hat.level_db, a, b)
        in_bar = beats[(beats >= a) & (beats < b - 0.5 * beat_s)]
        out["pulse"][i] = min(_pulse_db(low_t, low_db, in_bar, beat_s), 30.0)
        out["hat_pulse"][i] = min(_pulse_db(hat.times_s, hat.level_db, in_bar, beat_s), 40.0)
        frames = sp.frames_in(a, b)
        p = power[:, frames].sum(axis=1) if frames.any() else np.zeros(len(centres))
        out["low_share"][i] = bands.db(p[centres < 120].sum() / (p.sum() + bands.EPS))
        if sp.side is not None and frames.any():
            out["width"][i] = bands.db(sp.side[:, frames].sum() / (sp.mid[:, frames].sum() + bands.EPS))
        shape = bands.db(p[shape_band] + bands.EPS)
        out["shape"][i] = shape - shape.mean()
    return out


def _zscore(v: np.ndarray) -> np.ndarray:
    return (v - v.mean()) / (v.std() + 1e-9)


def _change(feats: dict[str, np.ndarray]) -> np.ndarray:
    """At each bar start: how different the CHANGE_BARS bars after it are from those before it."""
    x = np.stack([_zscore(feats[k]) * w for k, w in CHANGE_WEIGHTS.items()], axis=1)
    shape, n = feats["shape"], len(x)
    out = np.zeros(n)
    for b in range(1, n):
        lo, hi = max(0, b - CHANGE_BARS), min(n, b + CHANGE_BARS)
        before, after = shape[lo:b].mean(axis=0), shape[b:hi].mean(axis=0)
        cosine = 1 - before @ after / (np.linalg.norm(before) * np.linalg.norm(after) + 1e-9)
        out[b] = np.linalg.norm(x[lo:b].mean(axis=0) - x[b:hi].mean(axis=0)) + SHAPE_WEIGHT * 10 * cosine
    return out


def _cuts(change: np.ndarray, offset: int) -> set[int]:
    """Bars on the phrase grid whose change stands out (z-scored over the grid) and beats its neighbours a phrase away."""
    grid_bars = [b for b in range(offset or PHRASE, len(change) - 2, PHRASE) if b >= 2]
    if len(grid_bars) < 2:
        return set()
    z = _zscore(change[grid_bars])
    return {b for i, b in enumerate(grid_bars)
            if z[i] > CHANGE_Z and all(z[i] >= z[j] for j in (i - 1, i + 1) if 0 <= j < len(grid_bars))}


def _labels(feats: dict[str, np.ndarray], offset: int) -> tuple[np.ndarray, np.ndarray]:
    """A label and a confidence per bar: drops, then builds, then breakdowns; "" for the rest."""
    low, loud, pulse, hat_pulse = feats["low"], feats["loud"], feats["pulse"], feats["hat_pulse"]
    n = len(low)
    full_low = np.percentile(low, 95)
    full = (low >= full_low - FULL_DB) & (pulse >= PULSE_MIN_DB)
    label = np.array([""] * n, dtype=object)
    sure = np.zeros(n, dtype=bool)
    drops, b = [], MIN_DROP_BAR
    while b < n:
        comeback = low[b] - low[max(0, b - COMEBACK_BARS):b].min()
        if full[b] and comeback >= COMEBACK_DB and full[b:b + 4].mean() >= 0.5:
            start = min((x for x in (b - 1, b, b + 1) if (x - offset) % PHRASE == 0 and 0 <= x < n),
                        default=b, key=lambda x: abs(x - b))
            end, gap = start, 0
            while end < n and end - start < DROP_MAX_BARS:  # until the low end has been down DROP_GAP_BARS bars
                gap = 0 if low[end] >= full_low - DROP_END_DB else gap + 1
                if gap > DROP_GAP_BARS:
                    end -= gap - 1
                    break
                end += 1
            end = min(n, start + max(8, int(round((end - start) / 8)) * 8))  # 8, 16 bars
            label[start:end] = "drop"
            sure[start:end] = comeback >= 2 * COMEBACK_DB
            drops.append(start)
            b = end + 1
        else:
            b += 1
    for start in drops:  # the longest stretch before the drop (16, 8 or 4 bars) where the low end thins out
        for length in (16, 8, 4):
            a = start - length
            if a < 0 or (label[a:start] != "").any():
                continue
            seg = low[a:start]
            slope = np.polyfit(np.arange(length), seg, 1)[0]
            if slope <= -BUILD_SLOPE_DB or seg[-2:].mean() <= full_low - BUILD_DB:
                label[a:start] = "build"
                sure[a:start] = slope <= -2 * BUILD_SLOPE_DB
                break
    quiet = (label == "") & (((pulse < BREAKDOWN_PULSE_DB) & (hat_pulse < BREAKDOWN_HAT_DB))
                             | (low < full_low - BREAKDOWN_LOW_DB))
    i = 0
    while i < n:
        j = i
        while j < n and quiet[j]:
            j += 1
        if j - i >= BREAKDOWN_BARS:
            label[i:j] = "breakdown"
            sure[i:j] = (pulse[i:j] < BREAKDOWN_PULSE_DB / 2).mean() > 0.5
        i = max(j, i + 1)
    return label, sure


def propose(track: TrackData) -> list[Section]:
    starts = bar_starts(track)
    duration = track.audio.duration_s
    bar_s = 4 * track.features.grid.beat_s
    if len(starts) < MIN_BARS:
        return [Section("s1", "unknown", 0.0, duration, 0, bar_index(duration, starts, bar_s) if len(starts) else 0, "guess")]

    feats = bar_features(track, starts)
    change = _change(feats)
    # Phrase offset: the 4-bar alignment whose bar starts carry the most change (tracks often open with a pickup).
    offset = int(np.argmax([change[k::PHRASE].sum() for k in range(PHRASE)]))
    label, sure = _labels(feats, offset)
    cuts = _cuts(change, offset)
    n = len(starts)
    pieces, a = [], 0
    for b in range(1, n + 1):  # runs of one label; unlabelled runs also split at the biggest changes
        if b == n or label[b] != label[a] or (label[a] == "" and b in cuts):
            pieces.append([a, b, label[a] or "verse", bool(sure[a]) if label[a] else False])
            a = b
    if pieces[0][2] in ("verse", "breakdown"):
        pieces[0][2:] = ["intro", True]
    last_drop = max((k for k, s in enumerate(pieces) if s[2] == "drop"), default=None)
    if last_drop is not None:
        for s in pieces[last_drop + 1:]:
            if s[2] == "verse":
                s[2:] = ["outro", True]
    merged: list[list] = []
    for s in pieces:  # neighbours with one label become one section (builds stay apart)
        if merged and merged[-1][2] == s[2] and s[2] != "build":
            merged[-1][1] = s[1]
            merged[-1][3] = merged[-1][3] and s[3]
        else:
            merged.append(list(s))
    out = []
    for i, (a, b, lab, likely) in enumerate(merged):
        start_s = 0.0 if a == 0 else float(starts[a])
        end_s = duration if b >= n else float(starts[b])
        start_bar = (1 if starts[0] <= grid.START_TOLERANCE_S else 0) if a == 0 else a + 1
        out.append(Section(f"s{i + 1}", lab, round(start_s, 3), round(end_s, 3), start_bar, b + 1,
                           "likely" if likely else "guess"))
    return out
