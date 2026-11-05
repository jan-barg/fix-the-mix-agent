"""Waveform peaks for the section editor (DESIGN.md §9): the browser never decodes the full file.

100 buckets per second of the mid signal: min/max as int8, plus low (<200 Hz), mid (200 Hz-2 kHz)
and high (>2 kHz) RMS as 0-255 so the editor can colour the waveform by band, Rekordbox-style.
Cached in storage per file hash and analysis configuration.
"""

import base64
import json

import numpy as np
from scipy import signal as sps

from app.audio.decode import PreparedAudio
from app.audio.features import mid_signal
from app.config import settings
from app.storage import Storage

RATE = 100  # buckets per second
SPLITS_HZ = (200.0, 2000.0)


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def compute(audio: PreparedAudio) -> dict:
    sr = audio.sample_rate
    mid = mid_signal(audio.signal).astype(np.float32)
    hop = sr // RATE
    n = len(mid) // hop
    frames = mid[: n * hop].reshape(n, hop)
    lo, hi = SPLITS_HZ
    bands = {
        "low": sps.sosfilt(sps.butter(4, lo, "lowpass", fs=sr, output="sos"), mid),
        "mid": sps.sosfilt(sps.butter(4, [lo, hi], "bandpass", fs=sr, output="sos"), mid),
        "high": sps.sosfilt(sps.butter(4, hi, "highpass", fs=sr, output="sos"), mid),
    }
    out = {
        "rate": RATE,
        "length": n,
        "duration_s": audio.duration_s,
        "min": _b64(np.clip(np.round(frames.min(axis=1) * 127), -127, 127).astype(np.int8)),
        "max": _b64(np.clip(np.round(frames.max(axis=1) * 127), -127, 127).astype(np.int8)),
    }
    for name, x in bands.items():
        rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1))
        scale = np.percentile(rms, 99.5) or 1.0
        out[name] = _b64(np.clip(rms / scale * 255, 0, 255).astype(np.uint8))
    return out


def cache_path(sha256: str) -> str:
    return f"cache/peaks/{sha256}-{settings.config_version}.json"


def get_or_compute(audio: PreparedAudio, sha256: str, storage: Storage) -> dict:
    path = cache_path(sha256)
    if storage.exists(path):
        return json.loads(storage.read(path))
    peaks = compute(audio)
    storage.write(path, json.dumps(peaks).encode(), content_type="application/json")
    return peaks
