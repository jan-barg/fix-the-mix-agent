"""Probe, decode and peak-normalise uploaded audio (SPEC §5.3, §6.3).

Decoding goes through FFmpeg to float32 so decoded overs (MP3s often exceed full scale) are kept,
and every track is normalised once, with one gain for all channels, to 0 dBFS sample peak.
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Below this sample peak (-120 dBFS) a track counts as silent and cannot be normalised.
SILENCE_PEAK = 1e-6
# The codec each accepted extension must actually contain (ffprobe codec_name prefix): a FLAC or AAC
# file renamed .wav would otherwise be analysed and then served to the browser under the wrong type.
CODECS = {".wav": "pcm_", ".mp3": "mp3"}
PROBE_TIMEOUT_S = 30
DECODE_TIMEOUT_S = 180  # wall clock; the output length is capped separately with -t

log = logging.getLogger(__name__)

class AudioError(Exception):
    """A file that cannot be used, with a reason that is safe to show the user."""


@dataclass(frozen=True)
class ProbeInfo:
    format_name: str
    codec: str
    sample_rate: int
    channels: int
    duration_s: float
    bit_rate: int | None


@dataclass
class PreparedAudio:
    signal: np.ndarray  # (channels, samples) float32, peak-normalised unless silent
    sample_rate: int
    probe: ProbeInfo
    original_peak_dbfs: float | None  # None for a silent track
    applied_gain_db: float | None  # None when normalisation was unavailable
    silent: bool
    resampled_from: int | None
    decode_warnings: list[str] = field(default_factory=list)

    @property
    def channels(self) -> int:
        return self.signal.shape[0]

    @property
    def duration_s(self) -> float:
        return self.signal.shape[1] / self.sample_rate


def probe(path: Path) -> ProbeInfo:
    """Read the first audio stream's properties. Embedded cover art and other streams are ignored."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_streams", "-show_format", "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise AudioError("Reading this file took too long. Please check that it plays, then export it again as WAV or MP3.") from None
    if proc.returncode != 0:
        log.warning("ffprobe failed on %s: %s", path.name, proc.stderr.strip()[:300])
        raise AudioError("The file could not be read as audio. Please check that it plays, then upload it as WAV or MP3.")
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams") or []
    if not streams:
        raise AudioError("The file contains no audio stream.")
    stream, fmt = streams[0], info.get("format", {})
    duration = float(stream.get("duration") or fmt.get("duration") or 0.0)
    bit_rate = stream.get("bit_rate") or fmt.get("bit_rate")
    return ProbeInfo(
        format_name=fmt.get("format_name", ""),
        codec=stream.get("codec_name", ""),
        sample_rate=int(stream.get("sample_rate") or 0),
        channels=int(stream.get("channels") or 0),
        duration_s=duration,
        bit_rate=int(bit_rate) if bit_rate else None,
    )


def decode(path: Path, info: ProbeInfo, sample_rate: int, max_seconds: float | None = None) -> tuple[np.ndarray, list[str]]:
    """Decode the first audio stream to (channels, samples) float32 at sample_rate.

    The channel count is kept as is: mono stays mono, stereo stays stereo. max_seconds caps the
    decoded length, so a file whose container hides its real duration cannot fill memory.
    """
    limit = ["-t", f"{max_seconds:.3f}"] if max_seconds else []
    cmd = [
        "ffmpeg", "-nostdin", "-v", "warning", "-i", str(path), "-map", "0:a:0", *limit,
        "-f", "f32le", "-acodec", "pcm_f32le", "-ar", str(sample_rate), "-ac", str(info.channels), "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=DECODE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise AudioError("Decoding this file took too long. Please check that it plays, then export it again as WAV or MP3.") from None
    warnings = [line for line in proc.stderr.decode(errors="replace").splitlines() if line.strip()]
    if proc.returncode != 0:
        raise AudioError(f"Decoding failed: {(warnings or ['unknown error'])[-1][:200]}")
    samples = np.frombuffer(proc.stdout, dtype="<f4")
    usable = samples.size - samples.size % info.channels
    return samples[:usable].reshape(-1, info.channels).T.copy(), warnings


def peak_normalise(signal: np.ndarray) -> tuple[np.ndarray, float | None, float | None]:
    """Apply one constant gain so the largest sample across all channels sits at 0 dBFS.

    Returns (normalised signal, original peak dBFS, applied gain dB). A silent signal comes back
    unchanged with both values None: it has no usable peak and is never divided by zero.
    """
    peak = float(np.max(np.abs(signal))) if signal.size else 0.0
    if peak < SILENCE_PEAK:
        return signal, None, None
    gain = 1.0 / peak
    peak_db = 20.0 * np.log10(peak)
    return (signal * np.float32(gain)).astype(np.float32), peak_db, -peak_db


def prepare(path: Path, sample_rate: int, min_duration_s: float = 0.0, max_duration_s: float = float("inf")) -> PreparedAudio:
    """Validate, decode, resample if needed and peak-normalise one file."""
    def check_duration(seconds: float) -> None:
        if not (min_duration_s <= seconds <= max_duration_s):
            raise AudioError(
                f"The track is {seconds:.0f} s long; tracks must be between "
                f"{min_duration_s:.0f} s and {max_duration_s / 60:.0f} minutes."
            )

    info = probe(path)
    expected = CODECS.get(path.suffix.lower())
    if expected and not info.codec.startswith(expected):
        raise AudioError(f"This file is named {path.suffix.lower()} but contains {info.codec or 'unknown'} audio. "
                         "Please export it as WAV or MP3.")
    if info.channels not in (1, 2):
        raise AudioError(f"Only mono or stereo audio is supported; this file has {info.channels} channels.")
    if info.duration_s:
        check_duration(info.duration_s)  # reject long files before spending time decoding them

    cap = max_duration_s + 1.0 if np.isfinite(max_duration_s) else None
    raw, warnings = decode(path, info, sample_rate, cap)
    if cap and raw.shape[1] / sample_rate >= cap - 0.01:
        raise AudioError(f"The track is longer than {max_duration_s / 60:.0f} minutes; tracks must be between "
                         f"{min_duration_s:.0f} s and {max_duration_s / 60:.0f} minutes.")
    check_duration(raw.shape[1] / sample_rate)  # the container's duration can be missing or wrong
    if not np.isfinite(raw).all():
        raise AudioError("The file contains invalid samples (NaN or infinity). Please export it again as WAV or MP3.")
    normalised, peak_db, gain_db = peak_normalise(raw)
    return PreparedAudio(
        signal=normalised,
        sample_rate=sample_rate,
        probe=info,
        original_peak_dbfs=peak_db,
        applied_gain_db=gain_db,
        silent=peak_db is None,
        resampled_from=info.sample_rate if info.sample_rate != sample_rate else None,
        decode_warnings=warnings,
    )
