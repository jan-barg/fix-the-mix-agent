"""Tonal balance: long-term average spectra of whole tracks or sections (SPEC §5.8, §6.3).

Primary curves come from the whole-track peak-normalised signal. A level-matched view, which
shifts a curve by one broadband offset to test whether a tonal difference persists, is secondary
and always labelled as such.
"""

from dataclasses import dataclass

import numpy as np

from app.audio import bands
from app.audio.features import Scope

# Range over which level matching equalises broadband power.
LEVEL_MATCH_RANGE_HZ = (100.0, 10000.0)


@dataclass
class TonalCurve:
    label: str  # e.g. "Your track, Drop 1"
    track_id: str
    bands: bands.OctaveBands
    level_db: np.ndarray  # 1/6-octave power-averaged level, dB re full-scale mean square
    summary_db: dict[str, float]  # TONAL_SUMMARY_BANDS
    offset_db: float = 0.0  # non-zero only in the level-matched view


def long_term_spectrum(scope: Scope) -> TonalCurve:
    spectra = scope.track.features.spectra
    frames = spectra.frames_in(scope.start_s, scope.end_s)
    power = spectra.channel_average[:, frames].mean(axis=1)
    summary = bands.summary_band_power(spectra.bands, power, bands.TONAL_SUMMARY_BANDS)
    return TonalCurve(
        label=scope.describe(),
        track_id=scope.track.id,
        bands=spectra.bands,
        level_db=bands.db(power),
        summary_db={name: float(bands.db(p)) for name, p in summary.items()},
    )


def combined_target(curves: list[TonalCurve], label: str) -> TonalCurve:
    """Average of the contributors' dB curves (equal weights): a ballpark target, not a recording."""
    level = np.mean([c.level_db for c in curves], axis=0)
    summary = {k: float(np.mean([c.summary_db[k] for c in curves])) for k in curves[0].summary_db}
    return TonalCurve(label=label, track_id="combined", bands=curves[0].bands, level_db=level, summary_db=summary)


def broadband_db(curve: TonalCurve, lo: float, hi: float) -> float:
    mask = (curve.bands.centres >= lo) & (curve.bands.centres < hi)
    return float(bands.db(np.sum(10.0 ** (curve.level_db[mask] / 10.0))))


def level_matched(curve: TonalCurve, reference: TonalCurve) -> TonalCurve:
    """Shift `curve` so its broadband power over LEVEL_MATCH_RANGE_HZ equals `reference`'s."""
    offset = broadband_db(reference, *LEVEL_MATCH_RANGE_HZ) - broadband_db(curve, *LEVEL_MATCH_RANGE_HZ)
    return TonalCurve(
        label=curve.label,
        track_id=curve.track_id,
        bands=curve.bands,
        level_db=curve.level_db + offset,
        summary_db={k: v + offset for k, v in curve.summary_db.items()},
        offset_db=offset,
    )
