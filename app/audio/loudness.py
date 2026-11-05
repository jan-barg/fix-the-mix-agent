"""EBU R128 loudness and peak metering through FFmpeg's ebur128 filter (SPEC §5.8).

The peak-normalised signal is piped to FFmpeg as float, so these numbers describe the analysis
copy, not the original upload. The filter reports every 100 ms; series timestamps are the end
of each 100 ms step.
"""

import subprocess
from dataclasses import dataclass

import numpy as np

STEP_S = 0.1
# ebur128 reports about -120.7 LUFS when a window is silent or not yet full; anything at or
# below the BS.1770 absolute gate (-70 LUFS) is treated as "no reading" rather than a level.
NO_READING_LUFS = -70.0


@dataclass
class LoudnessResult:
    times_s: np.ndarray  # end of each 100 ms step
    momentary_lufs: np.ndarray  # 400 ms window, NaN where there is no reading
    short_term_lufs: np.ndarray  # 3 s window, NaN where there is no reading
    integrated_lufs: float | None  # None when no block passed the gates
    lra_lu: float | None
    sample_peak_dbfs: list[float]  # per channel
    true_peak_dbtp: list[float]  # per channel

    @property
    def max_sample_peak_dbfs(self) -> float:
        return max(self.sample_peak_dbfs)

    @property
    def max_true_peak_dbtp(self) -> float:
        return max(self.true_peak_dbtp)


def _db(linear: float) -> float:
    return 20.0 * np.log10(linear) if linear > 0 else float("-inf")


def _reading(value: float) -> float:
    return value if value > NO_READING_LUFS else np.nan


def measure(signal: np.ndarray, sample_rate: int) -> LoudnessResult:
    """Meter a (channels, samples) float32 signal. Mono and stereo only."""
    channels = signal.shape[0]
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-f", "f32le", "-ar", str(sample_rate), "-ac", str(channels), "-i", "pipe:0",
        "-af", "ebur128=metadata=1:peak=true+sample,ametadata=mode=print:file=/dev/stdout",
        "-f", "null", "-",
    ]
    interleaved = np.ascontiguousarray(signal.T, dtype="<f4").tobytes()
    proc = subprocess.run(cmd, input=interleaved, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ebur128 failed: {proc.stderr.decode(errors='replace')[-300:]}")
    return parse(proc.stdout.decode(), channels)


def integrated_from_momentary(momentary_lufs: np.ndarray) -> float | None:
    """BS.1770 gated integrated loudness from the 100 ms momentary series.

    Each momentary reading is one 400 ms gating block with 75% overlap, exactly the blocks the
    standard integrates, so this gives integrated loudness for any time range (a section).
    NaN readings are already below the absolute gate.
    """
    blocks = momentary_lufs[~np.isnan(momentary_lufs)]
    if blocks.size == 0:
        return None
    power = 10.0 ** ((blocks + 0.691) / 10.0)
    relative_gate = -0.691 + 10.0 * np.log10(power.mean()) - 10.0
    kept = power[blocks > relative_gate]
    return float(-0.691 + 10.0 * np.log10(kept.mean())) if kept.size else None


def true_peak_dbtp(segment: np.ndarray) -> float:
    """Estimated true peak of a (channels, samples) segment via 4x oversampling (BS.1770 Annex 2)."""
    from scipy.signal import resample_poly

    peak = max(float(np.max(np.abs(resample_poly(ch, 4, 1)))) for ch in segment) if segment.size else 0.0
    return _db(peak)


def parse(text: str, channels: int) -> LoudnessResult:
    """Parse ametadata's print output: a 'frame:' line followed by lavfi.r128.* key=value lines."""
    times, momentary, short_term = [], [], []
    last: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("frame:"):
            pts_time = float(line.rsplit("pts_time:", 1)[1])
            times.append(pts_time + STEP_S)
            continue
        if not line.startswith("lavfi.r128."):
            continue
        key, _, value = line[len("lavfi.r128."):].partition("=")
        number = float(value)
        if key == "M":
            momentary.append(number)
        elif key == "S":
            short_term.append(number)
        else:
            last[key] = number  # I, LRA and peaks are cumulative; the final frame holds the result

    if not times:
        raise RuntimeError("ebur128 produced no readings (the signal may be too short).")

    integrated = last.get("I")
    return LoudnessResult(
        times_s=np.array(times),
        momentary_lufs=np.array([_reading(v) for v in momentary]),
        short_term_lufs=np.array([_reading(v) for v in short_term]),
        integrated_lufs=integrated if integrated is not None and integrated > NO_READING_LUFS else None,
        lra_lu=last.get("LRA"),
        sample_peak_dbfs=[_db(last.get(f"sample_peaks_ch{c}", 0.0)) for c in range(channels)],
        true_peak_dbtp=[_db(last.get(f"true_peaks_ch{c}", 0.0)) for c in range(channels)],
    )
