"""Per-bar features for the section lab. app/audio/sections.py has the same ones, minus those the tuning left at zero."""
import numpy as np

from app.audio import bands, sections
from app.audio.features import TrackData, mid_signal

HAT_BAND = (6000.0, 16000.0)
PHASE_BINS = 24  # per beat


def _level_db(env: bands.Envelope, a: float, b: float) -> float:
    m = (env.times_s >= a) & (env.times_s < b)
    return float(bands.db(np.mean(10 ** (env.level_db[m] / 10)))) if m.any() else -90.0


def _beat_profile(times: np.ndarray, level_db: np.ndarray, beats: np.ndarray, beat_s: float) -> np.ndarray:
    """Median level across the given beats at PHASE_BINS points through the beat (NaN where a beat runs off the end)."""
    if not len(beats):
        return np.full(PHASE_BINS, np.nan)
    phase = np.arange(PHASE_BINS) / PHASE_BINS * beat_s
    pts = beats[:, None] + phase[None, :]
    vals = np.interp(pts, times, level_db, left=np.nan, right=np.nan)
    return np.nanmedian(vals, axis=0)


def _pulse(profile: np.ndarray) -> float:
    """How much the level swings within a beat: the same wherever in the beat the peak sits (kick on the beat, bass
    on the offbeat, or a grid that has drifted)."""
    p = profile[np.isfinite(profile)]
    return float(np.percentile(p, 95) - np.percentile(p, 5)) if p.size > 4 else 0.0


def bar_features(track: TrackData) -> dict:
    f = track.features
    starts = sections.bar_starts(track)
    ends = np.append(starts[1:], track.audio.duration_s)
    beats, beat_s = f.grid.beats_s, f.grid.beat_s
    env = f.envelopes
    low_power = 10 ** (env["sub"].level_db / 10) + 10 ** (env["low"].level_db / 10)
    low = bands.Envelope(env["sub"].times_s, bands.db(low_power), (30.0, 120.0))
    hat = bands.band_envelope(mid_signal(track.audio.signal), track.audio.sample_rate, HAT_BAND, smooth_s=0.01)
    L, sp = f.loudness, f.spectra
    centres = sp.bands.centres
    power = sp.channel_average
    shape_band = (centres >= 200) & (centres <= 5000)
    n = len(starts)
    out = {k: np.zeros(n) for k in ("loud", "low", "sub", "upper", "hat", "pulse", "hat_pulse", "low_share",
                                     "bright", "width")}
    out["shape"] = np.zeros((n, int(shape_band.sum())))
    for i, (a, b) in enumerate(zip(starts, ends)):
        out["loud"][i] = sections._power_mean_db(L.times_s, L.momentary_lufs, a, b)
        out["low"][i] = _level_db(low, a, b)
        out["sub"][i] = _level_db(env["sub"], a, b)
        out["upper"][i] = _level_db(env["upper_bass"], a, b)
        out["hat"][i] = _level_db(hat, a, b)
        in_bar = beats[(beats >= a) & (beats < b - 0.5 * beat_s)]
        out["pulse"][i] = _pulse(_beat_profile(low.times_s, low.level_db, in_bar, beat_s))
        out["hat_pulse"][i] = _pulse(_beat_profile(hat.times_s, hat.level_db, in_bar, beat_s))
        frames = sp.frames_in(a, b)
        p = power[:, frames].sum(axis=1) if frames.any() else np.zeros(len(centres))
        total = p.sum() + bands.EPS
        out["low_share"][i] = bands.db(p[centres < 120].sum() / total)
        out["bright"][i] = float(np.sum(np.log2(centres) * p) / total)
        if sp.side is not None and frames.any():
            out["width"][i] = bands.db(sp.side[:, frames].sum() / (sp.mid[:, frames].sum() + bands.EPS))
        s = bands.db(p[shape_band] + bands.EPS)
        out["shape"][i] = s - s.mean()
    out["starts"], out["ends"] = starts, ends
    return out
