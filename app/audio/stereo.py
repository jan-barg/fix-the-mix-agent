"""Stereo distribution and mono behaviour (SPEC §5.9).

Conventions (recorded with every result):
- mid = (L+R)/2, side = (L-R)/2.
- Side-to-mid ratio per band = 10·log10(P_side / P_mid).
- Mono change per band = 10·log10(P_mid / ((P_L + P_R)/2)): what folding to mono with the (L+R)/2
  convention does to that band's level. 0 dB for identical channels, about -3 dB for unrelated
  channels, strongly negative for opposite polarity (cancellation). The -3 dB of unrelated content
  is the fold-down convention, not loss; only values well below it indicate cancellation.
- Correlation = Re(L·R*) / sqrt(P_L·P_R) per band, and the L/R correlation coefficient over
  400 ms windows in the time domain.
Nothing is renormalised after the fold-down.
"""

from dataclasses import dataclass

import numpy as np

from app.audio import bands
from app.audio.features import Scope

CORRELATION_WINDOW_S = 0.4
# Below this mid level (dB re full scale) a band counts as silent and its ratios are undefined.
SILENT_BAND_DB = -100.0
CANCELLATION_DB = -6.0  # mono change below this suggests cancellation, not just fold-down scaling
REGIONS = {"low (below 120 Hz)": (20.0, 120.0), "mids (120 Hz to 2 kHz)": (120.0, 2000.0),
           "highs (above 2 kHz)": (2000.0, 20000.0)}


@dataclass
class StereoResult:
    label: str
    track_id: str
    bands: bands.OctaveBands  # 1/3 octave
    side_to_mid_db: np.ndarray  # NaN in silent bands
    correlation: np.ndarray
    mono_change_db: np.ndarray
    window_times_s: np.ndarray
    window_correlation: np.ndarray
    region_side_to_mid_db: dict[str, float]
    region_mono_change_db: dict[str, float]


def _ratio_db(num: np.ndarray, den: np.ndarray, valid: np.ndarray, floor_db: float = -60.0) -> np.ndarray:
    """Ratio in dB where valid, NaN elsewhere; clamped to ±60 dB so full cancellation stays finite."""
    out = np.full(num.shape, np.nan)
    out[valid] = np.clip(10.0 * np.log10(np.maximum(num[valid], bands.EPS) / np.maximum(den[valid], bands.EPS)),
                         floor_db, -floor_db)
    return out


def analyse(scope: Scope) -> StereoResult | None:
    """None for mono input: a mono file is valid but has no measurable width."""
    spectra = scope.track.features.spectra
    if spectra.right is None:
        return None
    frames = spectra.frames_in(scope.start_s, scope.end_s)
    totals = {name: getattr(spectra, name)[:, frames].sum(axis=1) for name in ("left", "right", "mid", "side", "cross")}
    third, merged = None, {}
    for name, power in totals.items():
        third, merged[name] = bands.merge_bands(spectra.bands, power, 2)
    avg_lr = (merged["left"] + merged["right"]) / 2
    # Silence is judged on the channels, not the mid, so full cancellation still counts as a reading.
    valid = bands.db(avg_lr / max(frames.sum(), 1)) > SILENT_BAND_DB

    correlation = np.full(avg_lr.shape, np.nan)
    lr = np.sqrt(merged["left"] * merged["right"])
    ok = valid & (lr > 0)
    correlation[ok] = merged["cross"][ok] / lr[ok]

    region_sm, region_mono = {}, {}
    for name, (lo, hi) in REGIONS.items():
        mask = (third.centres >= lo) & (third.centres < hi) & valid
        if not mask.any():
            region_sm[name] = region_mono[name] = float("nan")
            continue
        region_sm[name] = float(_ratio_db(merged["side"][mask].sum(keepdims=True), merged["mid"][mask].sum(keepdims=True), np.array([True]))[0])
        region_mono[name] = float(_ratio_db(merged["mid"][mask].sum(keepdims=True), avg_lr[mask].sum(keepdims=True), np.array([True]))[0])

    times, corr = window_correlation(scope)
    return StereoResult(
        label=scope.describe(),
        track_id=scope.track.id,
        bands=third,
        side_to_mid_db=_ratio_db(merged["side"], merged["mid"], valid),
        correlation=correlation,
        mono_change_db=_ratio_db(merged["mid"], avg_lr, valid),
        window_times_s=times,
        window_correlation=corr,
        region_side_to_mid_db=region_sm,
        region_mono_change_db=region_mono,
    )


def window_correlation(scope: Scope) -> tuple[np.ndarray, np.ndarray]:
    """L/R correlation coefficient (no mean removal, as on a phase-correlation meter) per 400 ms."""
    segment = scope.track.audio.signal[:, scope.samples()].astype(np.float64)
    win = int(CORRELATION_WINDOW_S * scope.track.audio.sample_rate)
    n = segment.shape[1] // win
    if n == 0:
        return np.array([]), np.array([])
    left, right = segment[0, : n * win].reshape(n, win), segment[1, : n * win].reshape(n, win)
    den = np.sqrt((left**2).sum(axis=1) * (right**2).sum(axis=1))
    corr = np.where(den > 0, (left * right).sum(axis=1) / np.where(den > 0, den, 1), np.nan)
    times = scope.start_s + (np.arange(n) + 0.5) * CORRELATION_WINDOW_S
    return times, corr
