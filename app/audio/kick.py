"""Kick finder and kick analysis (SPEC §5.6).

Goal: the fullest, most exposed kick anywhere in the track. Candidates are ranked on fullness and
isolation together, never on peak level alone (a big peak can be overlapping sources).
"""

from dataclasses import dataclass, field

import numpy as np
from scipy import signal as sps

from app.audio import bands, lowend
from app.audio.features import TrackData

BODY_BAND = (30.0, 120.0)
HF_BAND = (8000.0, 16000.0)  # above a typical kick click: hats and cymbals live here
CROP_PRE_S, CROP_S = 0.010, 0.400
MIN_GAP_S = 0.25
BODY_PEAK_WINDOW_S = 0.12  # a pitch-swept kick's sub energy can peak 100 ms in
OWN_ATTACK_S = 0.10  # onset peaks this soon after a kick belong to the kick itself
MIN_SPACING_S = 4.0  # spread chosen candidates through the track
# Low end "just before the kick" is read this long before the onset: earlier than that, the body
# envelope's smoothing (33 ms in the sub band) and zero-phase filtering already show the kick.
PRE_WINDOW_S = (0.090, 0.040)
OVERLAP_DB = 12.0  # body peak less than this above the low end before it: bass overlap likely


@dataclass
class KickCandidate:
    track_id: str
    time_s: float
    score: float
    fullness_db: float  # body peak relative to the track's strongest kicks (0 = as full as they get)
    isolation_db: float  # body peak above the low end just before the kick
    decay_drop_db: float | None  # body level drop 200-300 ms after the peak; None if the next onset is too close
    hf_contamination_db: float  # 8-16 kHz level during the kick relative to the track median
    gap_s: float  # time to the next onset of any kind
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _body_envelope(track: TrackData) -> bands.Envelope:
    env = track.features.envelopes
    power = 10 ** (env["sub"].level_db / 10) + 10 ** (env["low"].level_db / 10)
    return bands.Envelope(env["sub"].times_s, bands.db(power).astype(np.float32), BODY_BAND)


def _mean_db(times, level, lo, hi) -> float:
    mask = (times >= lo) & (times < hi)
    return float(bands.db(np.mean(10 ** (level[mask] / 10)))) if mask.any() else float("nan")


def find_candidates(track: TrackData, ranges: list[tuple[float, float]] | None = None, n: int = 5) -> list[KickCandidate]:
    """Rank the track's on-beat kicks (the grid's kick events), so bass notes and other off-grid
    onsets are never candidates. Any onset still ends the window in which a kick's decay is read."""
    f = track.features
    kicks = lowend.kick_events(track, 0.0, track.audio.duration_s)
    if len(kicks) < 2:
        return []
    onsets_idx, _ = sps.find_peaks(f.kick_onset_strength, distance=max(1, int(0.12 / (f.onset_times_s[1] - f.onset_times_s[0]))),
                                   height=np.percentile(f.kick_onset_strength, 75))
    onsets = f.onset_times_s[onsets_idx]
    body = _body_envelope(track)
    hf = bands.band_envelope(track.mid, track.audio.sample_rate, HF_BAND)
    hf_median = float(np.median(hf.level_db))

    peaks = np.array([np.max(body.level_db[(body.times_s >= t) & (body.times_s < t + BODY_PEAK_WINDOW_S)], initial=-200.0)
                      for t in kicks])
    before = np.array([_mean_db(body.times_s, body.level_db, t - PRE_WINDOW_S[0], t - PRE_WINDOW_S[1]) for t in kicks])
    isolations = peaks - before
    # Fullness is judged against the strongest of the better-isolated half of the kicks: a kick over a
    # bass note can read up to ~4 dB louder in the body band only because the bass adds to it.
    reference_peak = float(np.percentile(peaks[isolations >= np.median(isolations)], 95))

    candidates = []
    for i, t in enumerate(kicks):
        if ranges and not any(lo <= t < hi for lo, hi in ranges):
            continue
        later = onsets[onsets > t + OWN_ATTACK_S]
        gap = float(later[0] - t) if later.size else float(track.audio.duration_s - t)
        fullness = float(peaks[i] - reference_peak)
        isolation = float(isolations[i])
        decay = float(peaks[i] - _mean_db(body.times_s, body.level_db, t + 0.2, t + 0.3)) if gap >= 0.3 else None
        hf_cont = _mean_db(hf.times_s, hf.level_db, t, t + 0.15) - hf_median

        # Fullness first (the user wants the loud, substantial version), then isolation and an
        # inspectable decay. More than 3 dB below the strongest kicks is penalised hard enough that a
        # filtered intro kick cannot win on a clean decay alone.
        score = min(fullness, 0.0) + 6.0 * min(fullness + 3.0, 0.0) + 0.5 * min(isolation, 20.0) \
            + 0.3 * min(decay if decay is not None else 0.0, 30.0) - 0.3 * max(hf_cont, 0.0) \
            - (10.0 if gap < MIN_GAP_S else 0.0)
        c = KickCandidate(track.id, float(t), float(score), fullness, isolation, decay, float(hf_cont), gap)
        if fullness < -3:
            c.flags.append("weaker or filtered variation")
        if isolation < OVERLAP_DB:
            c.flags.append(f"low end just before the kick is only {isolation:.0f} dB below its peak (bass overlap likely)")
        if hf_cont > 6:
            c.flags.append("hats or cymbals present")
        if gap < MIN_GAP_S:
            c.flags.append("next onset too close to inspect the decay")
        c.reasons.append(f"body peak {abs(fullness):.1f} dB {'below' if fullness < 0 else 'above'} the track's strongest kicks")
        c.reasons.append(f"{isolation:.1f} dB above the low end just before it")
        if decay is not None:
            c.reasons.append(f"body falls {decay:.1f} dB within 300 ms")
        candidates.append(c)

    candidates.sort(key=lambda c: c.score, reverse=True)
    chosen: list[KickCandidate] = []
    for c in candidates:
        if all(abs(c.time_s - o.time_s) >= MIN_SPACING_S for o in chosen):
            chosen.append(c)
        if len(chosen) == n:
            break
    return chosen


@dataclass
class KickAnalysis:
    track_id: str
    time_s: float
    crop: np.ndarray  # mid signal, CROP_PRE_S before to CROP_S after the onset, parent normalisation
    sample_rate: int
    attack_body_db: float  # 2-8 kHz in the first 15 ms vs 40-120 Hz over 15-150 ms (mean-square ratio)
    decay_ms: float | None  # body falls 20 dB below its peak
    decay_note: str
    body_peak_hz: float
    body_centroid_hz: float
    pitch_start_hz: float | None
    pitch_end_hz: float | None
    pitch_note: str
    peak_dbfs: float
    rms_dbfs: float  # first 150 ms
    envelope_ms: np.ndarray
    envelope_db: np.ndarray  # body band envelope


def analyse(track: TrackData, time_s: float, next_onset_s: float | None = None) -> KickAnalysis:
    sr = track.audio.sample_rate
    start = int(max(0.0, time_s - CROP_PRE_S) * sr)
    crop = track.mid[start:start + int((CROP_PRE_S + CROP_S) * sr)].astype(np.float64)
    t0 = int(CROP_PRE_S * sr)

    def ms(a, b):
        return slice(t0 + int(a * sr / 1000), t0 + int(b * sr / 1000))

    attack = bands.bandpass(crop, sr, 2000.0, 8000.0)[ms(0, 15)]
    body = bands.bandpass(crop, sr, 40.0, 120.0)
    attack_body = float(bands.db(np.mean(attack**2)) - bands.db(np.mean(body[ms(15, 150)] ** 2)))

    env_t, env_db = bands.hilbert_envelope(bands.bandpass(crop, sr, *BODY_BAND), sr, smooth_s=0.005, hop_s=0.002)
    env_ms = env_t * 1000 - CROP_PRE_S * 1000
    i_peak = int(np.argmax(env_db))
    limit_ms = (next_onset_s - time_s) * 1000 if next_onset_s else CROP_S * 1000
    below = np.flatnonzero((np.arange(len(env_db)) > i_peak) & (env_db <= env_db[i_peak] - 20) & (env_ms < limit_ms))
    if below.size:
        decay_ms, decay_note = float(env_ms[below[0]] - env_ms[i_peak]), ""
    else:
        decay_ms = None
        decay_note = ("the body does not fall 20 dB before the next onset or the end of the 400 ms window "
                      "(sustained bass or a long tail)")

    seg = crop[ms(15, 150)] * np.hanning(len(crop[ms(15, 150)]))
    spec = np.abs(np.fft.rfft(seg, 16384)) ** 2
    freqs = np.fft.rfftfreq(16384, 1 / sr)
    body_mask, centroid_mask = (freqs >= 35) & (freqs <= 150), (freqs >= 30) & (freqs <= 300)
    body_peak = float(freqs[body_mask][np.argmax(spec[body_mask])])
    centroid = float(np.sum(freqs[centroid_mask] * spec[centroid_mask]) / np.sum(spec[centroid_mask]))

    # Pitch trajectory: spectral peak in 35-200 Hz per 46 ms window, 5 ms apart. Reported only when
    # the peak stands at least 6 dB above the band's median in most frames.
    win, hop = int(0.046 * sr), int(0.005 * sr)
    track_f, prominent = [], []
    pf = np.fft.rfftfreq(8192, 1 / sr)
    band = (pf >= 35) & (pf <= 200)
    for s in range(t0, min(len(crop) - win, t0 + int(0.15 * sr)), hop):
        frame = np.abs(np.fft.rfft(crop[s:s + win] * np.hanning(win), 8192)) ** 2
        b = frame[band]
        track_f.append(pf[band][np.argmax(b)])
        prominent.append(bands.db(b.max()) - bands.db(np.median(b)) >= 6)
    if track_f and np.mean(prominent) >= 0.7:
        f_arr = np.array(track_f)
        pitch_start, pitch_end, pitch_note = float(np.median(f_arr[:3])), float(np.median(f_arr[-5:])), \
            "46 ms analysis windows smooth the fastest part of the pitch drop"
    else:
        pitch_start = pitch_end = None
        pitch_note = "no clear tonal peak in the body; pitch not reported"

    return KickAnalysis(
        track_id=track.id, time_s=time_s, crop=crop.astype(np.float32), sample_rate=sr,
        attack_body_db=attack_body, decay_ms=decay_ms, decay_note=decay_note,
        body_peak_hz=body_peak, body_centroid_hz=centroid,
        pitch_start_hz=pitch_start, pitch_end_hz=pitch_end, pitch_note=pitch_note,
        peak_dbfs=float(bands.db(np.max(np.abs(crop)) ** 2)),
        rms_dbfs=float(bands.db(np.mean(crop[ms(0, 150)] ** 2))),
        envelope_ms=env_ms, envelope_db=env_db,
    )
