"""Frequency bands, fractional-octave spectra and band envelopes shared by every measurement.

Band definitions are configuration, recorded with each piece of evidence (SPEC §5.7: 300 Hz is a
starting point, not a boundary).
"""

from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import signal as sps
from scipy.fft import next_fast_len

# Low-end bands for envelopes and ducking analysis (Hz).
LOW_END_BANDS: dict[str, tuple[float, float]] = {
    "sub": (30.0, 60.0),
    "low": (60.0, 120.0),
    "upper_bass": (120.0, 300.0),
    "low_mid": (300.0, 600.0),
}

# Broad bands for summarising tonal balance (Hz).
TONAL_SUMMARY_BANDS: dict[str, tuple[float, float]] = {
    "sub": (20.0, 60.0),
    "bass": (60.0, 250.0),
    "low_mid": (250.0, 500.0),
    "mid": (500.0, 2000.0),
    "high_mid": (2000.0, 6000.0),
    "air": (6000.0, 20000.0),
}

# Long-term spectra: fine frequency resolution matters more than timing here.
SPECTRUM_N_FFT = 16384
SPECTRUM_HOP = 4096
SPECTRUM_FRACTION = 6  # 1/6-octave bands
SPECTRUM_FMIN, SPECTRUM_FMAX = 20.0, 20000.0

ENVELOPE_WINDOW_S = 0.010
ENVELOPE_HOP_S = 0.005

EPS = 1e-20


@dataclass(frozen=True)
class OctaveBands:
    centres: np.ndarray
    lower: np.ndarray
    upper: np.ndarray


def fractional_octave_bands(fraction: int = SPECTRUM_FRACTION, fmin: float = SPECTRUM_FMIN,
                            fmax: float = SPECTRUM_FMAX) -> OctaveBands:
    """Base-2 fractional-octave bands centred on 1 kHz, covering fmin to fmax."""
    k_min = int(np.ceil(fraction * np.log2(fmin / 1000.0)))
    k_max = int(np.floor(fraction * np.log2(fmax / 1000.0)))
    centres = 1000.0 * 2.0 ** (np.arange(k_min, k_max + 1) / fraction)
    half = 2.0 ** (1.0 / (2 * fraction))
    return OctaveBands(centres=centres, lower=centres / half, upper=centres * half)


def band_weights(freqs: np.ndarray, bands: OctaveBands) -> np.ndarray:
    """(n_bands, n_bins) weights: the fraction of each FFT bin's width that falls inside each band.

    Overlap weighting keeps narrow low-frequency bands (narrower than one bin) from coming out empty.
    """
    width = freqs[1] - freqs[0]
    bin_lo, bin_hi = freqs - width / 2, freqs + width / 2
    lo = np.maximum(bin_lo[None, :], bands.lower[:, None])
    hi = np.minimum(bin_hi[None, :], bands.upper[:, None])
    return (np.clip(hi - lo, 0.0, None) / width).astype(np.float32)


@dataclass
class BandSpectra:
    """Per-frame band powers for a stereo (or mono) signal.

    Power is mean-square amplitude re full scale (a full-scale sine reads -3 dB), so band powers
    add up to the signal's mean square. For mono input only `left` is set and the rest are None.
    """

    bands: OctaveBands
    times_s: np.ndarray  # frame centres
    left: np.ndarray  # (n_bands, n_frames)
    right: np.ndarray | None
    mid: np.ndarray | None  # (L+R)/2
    side: np.ndarray | None  # (L-R)/2
    cross: np.ndarray | None  # Re(L·conj(R)), for per-band correlation

    @property
    def channel_average(self) -> np.ndarray:
        """(|L|²+|R|²)/2: the tonal-balance view that keeps side content."""
        return self.left if self.right is None else (self.left + self.right) / 2

    @property
    def half_window_s(self) -> float:
        """A frame centred at t analyses t ± this (half of n_fft)."""
        step = float(self.times_s[1] - self.times_s[0]) if len(self.times_s) > 1 else 0.0
        return step * SPECTRUM_N_FFT / SPECTRUM_HOP / 2

    def frames_in(self, start_s: float, end_s: float) -> np.ndarray:
        """Frames whose whole window lies in [start_s, end_s]. Selecting by centre would let each
        186 ms half-window reach into the neighbouring section (a drop's first kick into the build).
        Empty for a range shorter than one window (372 ms at 44.1 kHz)."""
        h, eps = self.half_window_s, 1e-6
        return (self.times_s - h >= start_s - eps) & (self.times_s + h <= end_s + eps)


def band_spectra(audio: np.ndarray, sample_rate: int, block_frames: int = 256) -> BandSpectra:
    """STFT (Hann, n_fft 16384, hop 4096) aggregated into 1/6-octave band powers, in blocks to bound memory."""
    n_fft, hop = SPECTRUM_N_FFT, SPECTRUM_HOP
    window = sps.get_window("hann", n_fft).astype(np.float32)
    # Parseval scaling for a one-sided spectrum: sum over bins of scale·|X|² = mean square of the frame.
    scale = 2.0 / (n_fft * np.sum(window**2))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    bands = fractional_octave_bands()
    weights = band_weights(freqs, bands)

    padded = np.pad(audio, ((0, 0), (n_fft // 2, n_fft // 2)))
    frames = sliding_window_view(padded, n_fft, axis=1)[:, ::hop]  # (channels, n_frames, n_fft) view
    n_frames = frames.shape[1]
    stereo = audio.shape[0] == 2
    outputs = {name: np.empty((len(bands.centres), n_frames), np.float32)
               for name in (("left", "right", "mid", "side", "cross") if stereo else ("left",))}

    for start in range(0, n_frames, block_frames):
        stop = min(start + block_frames, n_frames)
        spectra = np.fft.rfft(frames[:, start:stop] * window, axis=-1)  # (channels, block, bins)
        left = spectra[0]
        powers = {"left": np.abs(left) ** 2}
        if stereo:
            right = spectra[1]
            powers["right"] = np.abs(right) ** 2
            powers["mid"] = np.abs((left + right) / 2) ** 2
            powers["side"] = np.abs((left - right) / 2) ** 2
            powers["cross"] = np.real(left * np.conj(right))
        for name, p in powers.items():
            outputs[name][:, start:stop] = (weights @ (p.T * scale)).astype(np.float32)

    return BandSpectra(
        bands=bands,
        times_s=np.arange(n_frames) * hop / sample_rate,
        left=outputs["left"],
        right=outputs.get("right"),
        mid=outputs.get("mid"),
        side=outputs.get("side"),
        cross=outputs.get("cross"),
    )


def merge_bands(bands: OctaveBands, power: np.ndarray, factor: int) -> tuple[OctaveBands, np.ndarray]:
    """Sum groups of `factor` adjacent bands (1/6 → 1/3 octave with factor 2)."""
    n = (len(bands.centres) // factor) * factor
    lower = bands.lower[:n:factor]
    upper = bands.upper[factor - 1:n:factor]
    merged = power[:n].reshape(n // factor, factor, *power.shape[1:]).sum(axis=1)
    return OctaveBands(centres=np.sqrt(lower * upper), lower=lower, upper=upper), merged


def summary_band_power(bands: OctaveBands, power: np.ndarray, summary: dict[str, tuple[float, float]]) -> dict[str, float]:
    """Total power of a 1-D band-power vector inside each summary band (bands assigned by centre)."""
    return {name: float(power[(bands.centres >= lo) & (bands.centres < hi)].sum()) for name, (lo, hi) in summary.items()}


def db(power, floor: float = EPS):
    return 10.0 * np.log10(np.maximum(power, floor))


# --- Band envelopes ---


@dataclass
class Envelope:
    times_s: np.ndarray  # window centres
    level_db: np.ndarray  # RMS level, dB re full scale (a full-scale sine reads -3 dB)
    band: tuple[float, float]


def bandpass(x: np.ndarray, sample_rate: int, lo: float, hi: float, order: int = 4) -> np.ndarray:
    """Zero-phase Butterworth band-pass, so envelope timing is not shifted by the filter."""
    sos = sps.butter(order, [lo, hi], btype="bandpass", fs=sample_rate, output="sos")
    return sps.sosfiltfilt(sos, x).astype(np.float32)


def rms_envelope(x: np.ndarray, sample_rate: int, window_s: float = ENVELOPE_WINDOW_S,
                 hop_s: float = ENVELOPE_HOP_S) -> tuple[np.ndarray, np.ndarray]:
    """Sliding RMS in dB (mean square → dB); returns (window centre times, level dB).

    Windows are centred on a fixed hop grid (shortened at the edges), so envelopes of one signal
    share a time axis whatever their window length.
    """
    win, hop = max(1, int(window_s * sample_rate)), max(1, int(hop_s * sample_rate))
    cumulative = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    centres = np.arange(0, len(x), hop)
    lo = np.clip(centres - win // 2, 0, len(x))
    hi = np.clip(centres + (win - win // 2), 0, len(x))
    mean_square = (cumulative[hi] - cumulative[lo]) / np.maximum(hi - lo, 1)
    return centres / sample_rate, db(mean_square)


def hilbert_envelope(y: np.ndarray, sample_rate: int, smooth_s: float, hop_s: float = ENVELOPE_HOP_S) -> tuple[np.ndarray, np.ndarray]:
    """Analytic-signal power envelope, averaged over smooth_s windows every hop_s, in dB.

    A plain RMS window shorter than one cycle ripples at the waveform's own frequency (a 10 ms window
    on a 40 Hz bass), which looks like level movement. The analytic signal's magnitude has no
    carrier ripple. |analytic|²/2 equals mean square, so a full-scale sine still reads -3 dB.
    """
    n = len(y)
    analytic = sps.hilbert(y, N=next_fast_len(n))[:n]
    return rms_envelope(np.sqrt(np.abs(analytic) ** 2 / 2), sample_rate, window_s=smooth_s, hop_s=hop_s)


def band_envelope(x: np.ndarray, sample_rate: int, band: tuple[float, float], smooth_s: float | None = None) -> Envelope:
    """Band-passed analytic envelope. Smoothing defaults to one period of the band's lowest frequency
    (at least 10 ms), which also sets the envelope's timing resolution."""
    smooth = smooth_s or max(ENVELOPE_WINDOW_S, 1.0 / band[0])
    times, level = hilbert_envelope(bandpass(x, sample_rate, *band), sample_rate, smooth)
    return Envelope(times_s=times, level_db=level.astype(np.float32), band=band)
