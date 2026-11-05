"""Tempo, beats and downbeats: the editing and alignment grid (SPEC §5.4).

The grid says where beats and bars are; it does not say what a section is. 4/4 is assumed.
Downbeats are a best guess the user can nudge in the section editor.
"""

from dataclasses import dataclass

import librosa
import numpy as np

ANALYSIS_SR = 22050
HOP = 512
FINE_HOP = 128  # 5.8 ms, for fitting tempo and phase
PHASE_BIN_S = 0.002  # folding many beats at 5.8 ms hops resolves the onset peak more finely
# Exactly one octave, so doubling or halving always lands inside (a 95 BPM track is reported as 95,
# a 92 BPM one as 184; the user can halve it in the editor).
BPM_RANGE = (95.0, 190.0)
# Below this coherence-weighted phase deviation the onsets support one straight grid.
CONSTANT_TEMPO_MAX_RESIDUAL_S = 0.020
# A beat this close before 0 s is the file's first beat. Files exported from bar 1 start on a beat,
# and the fitted phase scatters by up to about 20 ms either side of it (more when the first attack is
# clipped); wrapping a beat at -2 ms to the next period would drop bar 1 and shift every bar number.
START_TOLERANCE_S = 0.050


@dataclass
class Grid:
    bpm: float
    beats_s: np.ndarray
    downbeat_phase: int  # index of the first downbeat within beats_s (0-3)
    downbeat_confidence: str  # "likely" or "guess" when detected, "user" when set in the editor, "none" for the fallback
    constant_tempo: bool
    beat_residual_s: float  # RMS deviation of detected beats from the straight grid

    @property
    def downbeats_s(self) -> np.ndarray:
        return self.beats_s[self.downbeat_phase::4]

    @property
    def beat_s(self) -> float:
        return 60.0 / self.bpm

    def to_dict(self) -> dict:
        return {
            "bpm": round(self.bpm, 3),
            "beats_s": self.beats_s.round(4).tolist(),
            "downbeat_phase": self.downbeat_phase,
            "downbeat_confidence": self.downbeat_confidence,
            "constant_tempo": self.constant_tempo,
            "beat_residual_s": round(self.beat_residual_s, 4) if np.isfinite(self.beat_residual_s) else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Grid":
        return cls(bpm=d["bpm"], beats_s=np.array(d["beats_s"]), downbeat_phase=d["downbeat_phase"],
                   downbeat_confidence=d["downbeat_confidence"], constant_tempo=d["constant_tempo"],
                   beat_residual_s=float("nan") if d["beat_residual_s"] is None else d["beat_residual_s"])


def _fold_beats(beats: np.ndarray, onset_env: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Double or halve the beat rate until the tempo falls inside BPM_RANGE."""
    for _ in range(3):
        if len(beats) < 3:
            return beats
        bpm = 60.0 / np.median(np.diff(beats))
        if bpm < BPM_RANGE[0]:
            beats = np.sort(np.concatenate([beats, (beats[:-1] + beats[1:]) / 2]))
        elif bpm >= BPM_RANGE[1]:
            # Keep whichever alternate set of beats lands on stronger onsets.
            strength = [np.mean(np.interp(beats[k::2], times, onset_env)) for k in (0, 1)]
            beats = beats[int(np.argmax(strength))::2]
        else:
            break
    return beats


def _phase_strength(times: np.ndarray, weights: np.ndarray, periods: np.ndarray) -> np.ndarray:
    """Weighted sum of exp(2πi·t/P) per candidate period: its magnitude says how well onsets line
    up on a grid of that period, its angle says where the grid sits."""
    out = np.empty(len(periods), complex)
    for start in range(0, len(periods), 64):  # chunked to bound memory
        p = periods[start:start + 64, None]
        out[start:start + 64] = np.exp(2j * np.pi * times[None, :] / p) @ weights
    return out


def _straight_grid(y: np.ndarray, sr: int, bpm_estimate: float, duration_s: float):
    """Fit a constant-tempo grid at fine resolution.

    librosa's beat times are locked to analysis frames (23 ms at hop 512), which quantises its
    tempo: a 134 BPM track comes out as 136. Here a kick-weighted onset envelope at 5.8 ms
    resolution is folded onto candidate periods within ±4 BPM of the tracker's estimate, coarse
    then fine, and the best period and phase win. A whole-number BPM is preferred when it lines
    up the onsets almost as well, because DAW projects almost always use one.

    The grid's position comes from 64-beat windows (shorter for a track under 128 beats, so there
    are always at least two to compare), each weighted by how coherently its onsets line up (clean kick passages count most; busy bass lines, whose onsets fall between beats,
    count little). Returns (grid beats, bpm, weighted RMS phase deviation s, share of weight
    within 25 ms of the grid).
    """
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=FINE_HOP, n_mels=32, fmax=250.0)
    times = librosa.times_like(env, sr=sr, hop_length=FINE_HOP)
    weights = np.clip(env - np.median(env), 0.0, None)

    def strongest(candidates: np.ndarray) -> tuple[float, complex]:
        strength = _phase_strength(times, weights, 60.0 / candidates)
        i = int(np.argmax(np.abs(strength)))
        return float(candidates[i]), complex(strength[i])

    lo, hi = max(BPM_RANGE[0], bpm_estimate - 4.0), min(BPM_RANGE[1], bpm_estimate + 4.0)
    bpm, _ = strongest(np.arange(lo, hi, 0.01))
    bpm, best = strongest(np.arange(bpm - 0.02, bpm + 0.02, 0.0005))
    whole, at_whole = strongest(np.array([round(bpm)]))
    if abs(at_whole) >= 0.95 * abs(best):
        bpm, best = whole, at_whole
    period = 60.0 / bpm

    # Per-window phasors at the chosen period; coherence = how tightly that window's onsets align.
    phasors, coherence, masks = [], [], []
    window = max(min(64 * period, duration_s / 2), 8 * period)
    for start in np.arange(0.0, max(duration_s - window / 2, window / 2), window):
        mask = (times >= start) & (times < start + window)
        total = weights[mask].sum()
        if total <= 0:
            continue
        local = _phase_strength(times[mask], weights[mask], np.array([period]))[0]
        phasors.append(local / abs(local) if abs(local) > 0 else 0)
        coherence.append(abs(local) / total)
        masks.append(mask)
    phasors, w = np.array(phasors), np.array(coherence) ** 2
    if not len(phasors) or w.sum() == 0:
        offset = (np.angle(best) / (2 * np.pi)) * period % period
        return _beats_from(offset, period, duration_s), bpm, float("nan"), 0.0
    offset = (np.angle(np.sum(w * phasors)) / (2 * np.pi)) * period % period
    local_offsets = (np.angle(phasors) / (2 * np.pi)) * period % period
    deviations = (local_offsets - offset + period / 2) % period - period / 2
    rms = float(np.sqrt(np.sum(w * deviations**2) / w.sum()))
    agree = float(np.sum(w * (np.abs(deviations) < 0.025)) / w.sum())
    if len(phasors) < 2:
        # One window agrees with itself whatever the timing, so a short track proves nothing about tempo.
        rms, agree = float("nan"), 0.0

    frame_w = np.zeros_like(weights)
    for mask, wi in zip(masks, w):
        frame_w[mask] = wi
    offset = _onset_peak(times, weights * frame_w, period, offset)
    return _beats_from(offset, period, duration_s), bpm, rms, agree


def _beats_from(offset: float, period: float, duration_s: float) -> np.ndarray:
    """Straight beats at a phase in [0, period). The first beat may sit up to START_TOLERANCE_S before 0."""
    if offset > period - START_TOLERANCE_S:
        offset -= period
    return np.arange(offset, duration_s, period)


def _onset_peak(times: np.ndarray, weights: np.ndarray, period: float, offset: float) -> float:
    """Move the grid from the circular mean to where the onsets peak.

    The weighted circular mean is pulled late by onset energy that trails each attack (and by
    off-beat parts), by about 25 ms on a typical drop. Folding the envelope onto one beat and taking
    its peak within a quarter beat of the mean puts the grid lines on the kicks.
    """
    rel = (times - offset + period / 2) % period - period / 2
    edges = np.arange(-period / 4, period / 4 + PHASE_BIN_S, PHASE_BIN_S)
    idx = np.digitize(rel, edges) - 1
    inside = (idx >= 0) & (idx < len(edges) - 1)
    total = np.bincount(idx[inside], weights=weights[inside], minlength=len(edges) - 1)
    count = np.bincount(idx[inside], minlength=len(edges) - 1)
    profile = np.convolve(total / np.maximum(count, 1), np.ones(3) / 3, mode="same")
    if not profile.any():
        return offset
    shift = edges[int(np.argmax(profile))] + PHASE_BIN_S / 2
    return (offset + shift) % period


def _downbeat_phase(beats: np.ndarray, y: np.ndarray, sr: int) -> tuple[int, str]:
    """Pick which of every four beats starts a bar.

    A four-on-the-floor kick is the same on every beat, so the kick cannot tell bars apart. Bar
    starts are where things change: harmonic changes (chroma) and broadband onsets (crashes,
    new parts). The phase with the most change wins.
    """
    if len(beats) < 16:
        return 0, "guess"
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=HOP)
    frames = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, chroma.shape[1] - 1)
    # Column i: median chroma from beat i to beat i+1. (librosa.util.sync drops a boundary at frame 0
    # or past the end, which shifts every column by one beat.)
    bounds = np.append(frames, chroma.shape[1])
    synced = np.stack([np.median(chroma[:, a:max(b, a + 1)], axis=1) for a, b in zip(bounds[:-1], bounds[1:])], axis=1)
    harmonic_change = np.concatenate([[0.0], np.linalg.norm(np.diff(synced, axis=1), axis=0)])  # change arriving at beat i
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    onset_at_beat = onset[np.clip(frames, 0, len(onset) - 1)]

    def zscore(v):
        return (v - v.mean()) / (v.std() + 1e-9)

    change = zscore(harmonic_change) + zscore(onset_at_beat)
    scores = np.array([change[k::4].mean() for k in range(4)])
    best = int(np.argmax(scores))
    margin = np.sort(scores)[-1] - np.sort(scores)[-2]
    return best, "likely" if margin > 0.25 else "guess"


def _starts_on_beat(beats: np.ndarray, y: np.ndarray, sr: int) -> bool:
    """The first beat is at the file start and there is sound there (not a silent lead-in)."""
    head = y[: int(0.05 * sr)]
    return abs(beats[0]) <= START_TOLERANCE_S and head.size > 0 and float(np.mean(head**2)) > 1e-6  # above -60 dBFS


def fallback(duration_s: float) -> Grid:
    """Placeholder when no beat grid can be detected (no steady beat, or a silent mid signal):
    120 BPM from 0 s, with downbeat_confidence "none" so the editor asks the user to set it."""
    return Grid(bpm=120.0, beats_s=np.arange(0.0, duration_s, 0.5), downbeat_phase=0, downbeat_confidence="none",
                constant_tempo=False, beat_residual_s=float("nan"))


def analyse(mono: np.ndarray, sample_rate: int) -> Grid:
    """Tempo, beats and downbeat phase for a mono (mid) signal."""
    y = librosa.resample(mono, orig_sr=sample_rate, target_sr=ANALYSIS_SR) if sample_rate != ANALYSIS_SR else mono
    duration = len(y) / ANALYSIS_SR
    onset_env = librosa.onset.onset_strength(y=y, sr=ANALYSIS_SR, hop_length=HOP)
    times = librosa.times_like(onset_env, sr=ANALYSIS_SR, hop_length=HOP)
    _, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=ANALYSIS_SR, hop_length=HOP,
                                       start_bpm=125.0, units="time")
    beats = _fold_beats(np.asarray(beats, dtype=float), onset_env, times)
    if len(beats) < 8:
        raise ValueError("Too few beats detected to build a grid.")

    # Electronic tracks made in a DAW are almost always constant tempo, so the grid is always
    # straight; `constant_tempo` records whether the onsets actually support that, and the
    # editor flags the grid for checking when they do not.
    estimate = float(np.clip(60.0 / np.median(np.diff(beats)), BPM_RANGE[0], BPM_RANGE[1] - 0.01))
    straight, bpm, residual, agree = _straight_grid(y, ANALYSIS_SR, estimate, duration)
    constant = residual < CONSTANT_TEMPO_MAX_RESIDUAL_S and agree > 0.8
    phase, confidence = _downbeat_phase(straight, y, ANALYSIS_SR)
    if confidence == "guess" and _starts_on_beat(straight, y, ANALYSIS_SR):
        phase = 0  # tie-breaker: a file that starts with sound on a beat was almost always exported from bar 1
    return Grid(bpm=float(bpm), beats_s=straight, downbeat_phase=phase, downbeat_confidence=confidence,
                constant_tempo=constant, beat_residual_s=residual)


def user_grid(bpm: float, first_downbeat_s: float, duration_s: float) -> Grid:
    """A straight grid from the editor's BPM field and downbeat position."""
    period = 60.0 / bpm
    first = first_downbeat_s - period * 4 * np.floor(first_downbeat_s / (period * 4))
    if first > period * 4 - START_TOLERANCE_S:
        first -= period * 4
    pickup = int(np.floor((first + START_TOLERANCE_S) / period))  # beats before the first downbeat
    return Grid(bpm=bpm, beats_s=np.arange(first - pickup * period, duration_s, period), downbeat_phase=pickup,
                downbeat_confidence="user", constant_tempo=True, beat_residual_s=0.0)
