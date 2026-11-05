"""Low end: kick events, event-aligned band envelopes and observed ducking recovery (SPEC §5.7).

The output is *observed* recovery in a finished mix, never a compressor's release setting. The
criterion below is recorded with every estimate (DESIGN.md §5.5).

Single events are too noisy to measure (a bass line re-articulates inside most beats), so the
estimate comes from the median across kicks:

- each kick's band envelope, one beat long, is taken relative to its own baseline: the median level
  from 50% to 80% of the beat, which stops short of the next kick (zero-phase filtering and
  smoothing spread the next kick slightly backwards)
- dip: minimum of the median curve from the kick's own peak in that band (at least 30 ms after
  the onset) to 50% of the beat
- depth: baseline - dip (dB)
- recovery time: from the dip until the median curve is back within 10% of the depth from the
  baseline; its spread is a bootstrap over kicks
- unavailable when: fewer than 8 kicks, the median curve is not steady in the baseline window
  (10th-90th percentile range over 3 dB: a bass note or phrase change there), depth below 1.5 dB,
  the dip shows in fewer than 60% of kicks, or no recovery by 80% of the beat

A band-passed mix contains every source in that band. In the sub and low bands the kick itself
dominates, so upper-bass and harmonic bands are the better evidence of bass ducking.
"""

from dataclasses import dataclass, field

import numpy as np
from scipy import signal as sps

from app.audio import bands
from app.audio.features import Scope, TrackData

EVENT_SEARCH_S = 0.04  # a kick must start within ±40 ms of a beat
DIP_SEARCH_START_S = 0.030
KICK_PEAK_WINDOW_S = 0.150  # the kick's own peak in a band is looked for this soon after the onset
DIP_SEARCH_END = 0.50  # fraction of the beat
BASELINE_WINDOW = (0.50, 0.80)  # fraction of the beat
RECOVERY_FRACTION = 0.90
MIN_EVENTS = 8
MIN_DEPTH_DB = 1.5
MAX_BASELINE_RANGE_DB = 3.0  # 10th-90th percentile range of the median curve in the baseline window
MIN_CONSISTENT_SHARE = 0.6
BOOTSTRAP_ROUNDS = 200
PRE_EVENT_S = 0.02
CRITERION = (
    "Each kick's one-beat band envelope is taken relative to its own baseline (median level from 50% "
    "to 80% of the beat); the median across kicks is measured. Dip = minimum after the kick's own peak in "
    "that band (at least 30 ms after the onset) up to 50% of the beat; recovered = back within 10% of the dip depth of the baseline. Unavailable when "
    "that median moves more than 3 dB inside the baseline window, the dip is under 1.5 dB or shows in "
    "fewer than 60% of kicks, or there is no recovery by 80% of the beat."
)


def kick_events(track: TrackData, start_s: float, end_s: float) -> np.ndarray:
    """Kick onsets in a range: the strongest kick-weighted onset within ±40 ms of each grid beat,
    kept when it is at least 30% of the track's typical (90th percentile) on-beat kick strength
    and 10% of the track's strong onsets (99th percentile anywhere). The second floor stops a
    track without on-beat kicks from turning every beat's noise into a "kick"."""
    f = track.features
    times, strength = f.onset_times_s, f.kick_onset_strength
    beats = f.grid.beats_s
    on_beat = []
    for beat in beats:
        window = (times >= beat - EVENT_SEARCH_S) & (times <= beat + EVENT_SEARCH_S)
        if window.any():
            i = np.flatnonzero(window)[np.argmax(strength[window])]
            on_beat.append((beat, times[i], strength[i]))
    if not on_beat:
        return np.array([])
    floor = max(0.3 * np.percentile([s for _, _, s in on_beat], 90), 0.1 * np.percentile(strength, 99))
    return np.array([t for beat, t, s in on_beat if start_s <= beat < end_s and s > 0 and s >= floor])


@dataclass
class BandRecovery:
    band_name: str
    band_hz: tuple[float, float]
    available: bool
    reason: str  # why unavailable, or "" when available
    n_events: int
    depth_db: float | None = None  # on the median curve
    depth_iqr_db: float | None = None  # spread across kicks at the median dip time
    dip_ms: float | None = None  # after the kick
    recovery_ms: float | None = None  # from the dip
    recovery_iqr_ms: float | None = None  # bootstrap over kicks
    recovered_at_ms: float | None = None  # after the kick
    recovery_beats: float | None = None  # recovery_ms as a fraction of a beat
    shape: str | None = None
    baseline_spread_db: float | None = None  # IQR of per-kick baselines, context only
    consistent_share: float | None = None  # kicks at least half the median depth down at the dip time
    # Event-aligned envelope (relative to each event's baseline) for plotting.
    curve_time_ms: np.ndarray = field(default_factory=lambda: np.array([]))
    curve_median_db: np.ndarray = field(default_factory=lambda: np.array([]))
    curve_q1_db: np.ndarray = field(default_factory=lambda: np.array([]))
    curve_q3_db: np.ndarray = field(default_factory=lambda: np.array([]))


def _classify_shape(t_ms: np.ndarray, curve: np.ndarray, i_dip: int, i_rec: int, baseline: float,
                    depth: float, beat_ms: float) -> str:
    """Describe the median recovery: quick return, gradual swell or delayed return."""
    tail_t, tail = t_ms[i_dip:i_rec + 1] - t_ms[i_dip], curve[i_dip:i_rec + 1]
    t50, t90 = tail_t[np.flatnonzero(tail >= baseline - 0.5 * depth)[0]], tail_t[-1]
    if t90 < 0.2 * beat_ms:
        return "quick return"
    if t50 > 0.6 * t90:
        return "delayed return (stays low, then rises late)"
    return "gradual swell"


def _kick_peak(t_ms: np.ndarray, curve: np.ndarray) -> int:
    """Index where the kick's own rise in this band ends. In the sub band a pitch-swept kick body
    arrives late (often near 100 ms), so a dip searched before it would just be the kick's attack."""
    start = int(np.flatnonzero(t_ms >= 0)[0])
    peaks, _ = sps.find_peaks(curve[start:], prominence=1.0)
    early = start + peaks[t_ms[start + peaks] <= KICK_PEAK_WINDOW_S * 1000]
    # Only a peak the curve rises into from the onset is the kick's; a bump after a dip is the bass coming back.
    if early.size and curve[start:early[0] + 1].min() >= curve[start] - 1.0:
        return int(early[0])
    return start


def _measure(t_ms: np.ndarray, curve: np.ndarray, beat_ms: float) -> tuple[float, float, int, int | None]:
    """(baseline, depth, dip index, recovery index or None) on one event-aligned curve."""
    window = (t_ms >= BASELINE_WINDOW[0] * beat_ms) & (t_ms < BASELINE_WINDOW[1] * beat_ms)
    baseline = float(np.median(curve[window]))
    search_from = max(DIP_SEARCH_START_S * 1000, t_ms[_kick_peak(t_ms, curve)])
    search = np.flatnonzero((t_ms >= search_from) & (t_ms <= DIP_SEARCH_END * beat_ms))
    if not search.size:
        return baseline, 0.0, int(np.flatnonzero(t_ms >= search_from)[0]), None
    i_dip = int(search[np.argmin(curve[search])])
    depth = baseline - float(curve[i_dip])
    after = np.flatnonzero((np.arange(len(curve)) > i_dip) & (t_ms < BASELINE_WINDOW[1] * beat_ms)
                           & (curve >= baseline - (1 - RECOVERY_FRACTION) * depth))
    return baseline, depth, i_dip, (int(after[0]) if after.size else None)


def recovery(env: bands.Envelope, band_name: str, events: np.ndarray, beat_s: float) -> BandRecovery:
    times, level = env.times_s, env.level_db
    hop = times[1] - times[0]
    beat_ms = beat_s * 1000
    n_points = int(round((beat_s + PRE_EVENT_S) / hop))
    t_ms = (np.arange(n_points) * hop - PRE_EVENT_S) * 1000
    window = (t_ms >= BASELINE_WINDOW[0] * beat_ms) & (t_ms < BASELINE_WINDOW[1] * beat_ms)

    curves, baselines = [], []
    for ev in events:
        start = int(np.searchsorted(times, ev - PRE_EVENT_S))
        chunk = level[start:start + n_points].astype(np.float64)
        if len(chunk) == n_points:
            baselines.append(float(np.median(chunk[window])))
            curves.append(chunk - baselines[-1])
    result = BandRecovery(band_name=band_name, band_hz=env.band, available=False, reason="", n_events=len(curves))
    if len(curves) < MIN_EVENTS:
        result.reason = f"only {len(curves)} kick events in this passage (need {MIN_EVENTS})"
        return result

    c = np.array(curves)
    median = np.median(c, axis=0)
    result.curve_time_ms, result.curve_median_db = t_ms, median
    result.curve_q1_db, result.curve_q3_db = np.percentile(c, 25, axis=0), np.percentile(c, 75, axis=0)
    result.baseline_spread_db = float(np.subtract(*np.percentile(baselines, [75, 25])))

    movement = float(np.subtract(*np.percentile(median[window], [90, 10])))
    if movement > MAX_BASELINE_RANGE_DB:
        result.reason = (f"the level between the dip and the next kick moves by {movement:.1f} dB on average "
                         "(a bass note or phrase change there), so there is no defensible baseline")
        return result
    baseline, depth, i_dip, i_rec = _measure(t_ms, median, beat_ms)
    result.depth_db, result.dip_ms = depth, float(t_ms[i_dip])
    result.depth_iqr_db = float(np.subtract(*np.percentile(baseline - c[:, i_dip], [75, 25])))
    result.consistent_share = float(np.mean(c[:, i_dip] <= baseline - depth / 2))
    if depth < MIN_DEPTH_DB:
        result.reason = f"no clear dip after the kick (depth {depth:.1f} dB)"
        return result
    if result.consistent_share < MIN_CONSISTENT_SHARE:
        result.reason = f"the dip shows in only {result.consistent_share:.0%} of kicks"
        return result
    if i_rec is None:
        result.reason = "the level has not recovered by 80% of the beat"
        return result

    # Spread: resample kicks with replacement and re-measure the median curve.
    rng = np.random.default_rng(0)
    boot = []
    for _ in range(BOOTSTRAP_ROUNDS):
        _, _, b_dip, b_rec = _measure(t_ms, np.median(c[rng.integers(0, len(c), len(c))], axis=0), beat_ms)
        if b_rec is not None:
            boot.append(t_ms[b_rec] - t_ms[b_dip])
    result.available = True
    result.recovery_ms = float(t_ms[i_rec] - t_ms[i_dip])
    result.recovered_at_ms = float(t_ms[i_rec])
    result.recovery_iqr_ms = float(np.subtract(*np.percentile(boot, [75, 25]))) if boot else None
    result.recovery_beats = result.recovery_ms / beat_ms
    result.shape = _classify_shape(t_ms, median, i_dip, i_rec, baseline, depth, beat_ms)
    return result


@dataclass
class LowEndResult:
    label: str
    track_id: str
    events_s: np.ndarray
    bpm: float
    recoveries: dict[str, BandRecovery]
    distribution_bands: bands.OctaveBands
    distribution_db: np.ndarray  # 1/6-octave levels, 20-300 Hz
    criterion: str = CRITERION


def analyse(scope: Scope) -> LowEndResult:
    track = scope.track
    beat_s = track.features.grid.beat_s
    events = kick_events(track, scope.start_s, scope.end_s)
    # Each kick is read over one beat from 20 ms before it; one that runs past the scope would
    # measure the next section's low end.
    events = events[(events - PRE_EVENT_S >= scope.start_s) & (events + beat_s <= scope.end_s)]
    recs = {name: recovery(env, name, events, beat_s) for name, env in track.features.envelopes.items()}
    spectra = track.features.spectra
    frames = spectra.frames_in(scope.start_s, scope.end_s)
    keep = (spectra.bands.centres >= 20) & (spectra.bands.centres <= 300)
    power = (spectra.channel_average[:, frames].mean(axis=1) if frames.any()
             else np.full(len(spectra.bands.centres), np.nan))[keep]
    return LowEndResult(
        label=scope.describe(),
        track_id=track.id,
        events_s=events,
        bpm=track.features.grid.bpm,
        recoveries=recs,
        distribution_bands=bands.OctaveBands(spectra.bands.centres[keep], spectra.bands.lower[keep], spectra.bands.upper[keep]),
        distribution_db=bands.db(power),
    )
