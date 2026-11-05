"""Whole-track base analysis, computed once per file and analysis configuration (DESIGN.md §5.2).

Everything later measurements reuse lives here: loudness series, the beat grid, band spectra, low-end
envelopes, a kick-weighted onset envelope and waveform peaks. Results are cached in storage under
cache/features/<sha256>-<config_version>.npz, so demo tracks are analysed only once.
"""

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np

from app.audio import bands, grid, loudness
from app.audio.decode import PreparedAudio
from app.config import settings
from app.storage import Storage

ONSET_SR = 22050
ONSET_HOP = 128  # 5.8 ms
WAVEFORM_BUCKETS = 4000


@dataclass
class BaseFeatures:
    sha256: str
    config_version: str
    sample_rate: int
    duration_s: float
    channels: int
    loudness: loudness.LoudnessResult
    grid: grid.Grid
    spectra: bands.BandSpectra
    envelopes: dict[str, bands.Envelope]  # LOW_END_BANDS of the mid (or mono) signal
    onset_times_s: np.ndarray
    kick_onset_strength: np.ndarray  # kick-weighted (below 250 Hz) onset envelope
    waveform: np.ndarray  # (2, WAVEFORM_BUCKETS) min and max of the mid signal


@dataclass
class TrackData:
    """One uploaded track ready for measurement."""

    id: str  # "user", "ref1", "ref2", "ref3"
    name: str  # display name, e.g. "Your track" or "Reference 1"
    role: str  # "user" or "reference"
    audio: PreparedAudio
    features: BaseFeatures

    @property
    def mid(self) -> np.ndarray:
        return mid_signal(self.audio.signal)


def clock(t: float) -> str:
    """m:ss, as the section editor shows times."""
    return f"{int(t // 60)}:{int(t % 60):02d}"


@dataclass(frozen=True)
class Scope:
    """A time range of one track that a measurement covers."""

    track: TrackData
    start_s: float
    end_s: float
    label: str  # "Whole track", "drop", "kick at 12.34 s", ...
    section_id: str | None = None
    kind: str = "section"  # "whole" (the full file), "section" (a passage) or "event" (a kick crop)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def is_whole(self) -> bool:
        return self.kind == "whole"

    def describe(self) -> str:
        """Names the scope in evidence keys and captions. A section carries its time range, so two
        sections with the same label (two drops) never share a key."""
        if self.is_whole:
            return f"{self.track.name} (whole track)"
        if self.kind == "event":
            return f"{self.track.name}, {self.label}"
        return f"{self.track.name}, {self.label} {clock(self.start_s)}–{clock(self.end_s)}"

    def samples(self) -> slice:
        sr = self.track.audio.sample_rate
        return slice(int(self.start_s * sr), int(self.end_s * sr))


def whole_track(track: TrackData) -> Scope:
    return Scope(track, 0.0, track.audio.duration_s, "Whole track", kind="whole")


def mid_signal(signal: np.ndarray) -> np.ndarray:
    """(L+R)/2 for stereo, the signal itself for mono."""
    return signal[0] if signal.shape[0] == 1 else (signal[0] + signal[1]) / 2


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compute(audio: PreparedAudio, sha256: str) -> BaseFeatures:
    mid = mid_signal(audio.signal)
    sr = audio.sample_rate

    y = librosa.resample(mid, orig_sr=sr, target_sr=ONSET_SR)
    kick_env = librosa.onset.onset_strength(y=y, sr=ONSET_SR, hop_length=ONSET_HOP, n_mels=32, fmax=250.0)

    starts = np.linspace(0, len(mid), WAVEFORM_BUCKETS, endpoint=False).astype(int)
    waveform = np.stack([np.minimum.reduceat(mid, starts), np.maximum.reduceat(mid, starts)]).astype(np.float32)

    return BaseFeatures(
        sha256=sha256,
        config_version=settings.config_version,
        sample_rate=sr,
        duration_s=audio.duration_s,
        channels=audio.channels,
        loudness=loudness.measure(audio.signal, sr),
        grid=_grid(mid, sr, audio.duration_s),
        spectra=bands.band_spectra(audio.signal, sr),
        envelopes={name: bands.band_envelope(mid, sr, band) for name, band in bands.LOW_END_BANDS.items()},
        onset_times_s=librosa.times_like(kick_env, sr=ONSET_SR, hop_length=ONSET_HOP),
        kick_onset_strength=kick_env.astype(np.float32),
        waveform=waveform,
    )


def _grid(mid: np.ndarray, sr: int, duration_s: float) -> grid.Grid:
    try:
        return grid.analyse(mid, sr)
    except ValueError:  # no steady beat (an ambient piece, a spoken intro): the user sets the grid
        return grid.fallback(duration_s)


# --- Cache ---


def cache_path(sha256: str) -> str:
    return f"cache/features/{sha256}-{settings.config_version}.npz"


def to_bytes(f: BaseFeatures) -> bytes:
    s, l = f.spectra, f.loudness
    arrays = {
        "loud_times": l.times_s, "loud_m": l.momentary_lufs, "loud_s": l.short_term_lufs,
        "spec_centres": s.bands.centres, "spec_lower": s.bands.lower, "spec_upper": s.bands.upper,
        "spec_times": s.times_s, "spec_left": s.left,
        "onset_times": f.onset_times_s, "kick_onset": f.kick_onset_strength, "waveform": f.waveform,
    }
    for name in ("right", "mid", "side", "cross"):
        if getattr(s, name) is not None:
            arrays[f"spec_{name}"] = getattr(s, name)
    for name, env in f.envelopes.items():
        arrays[f"env_{name}_times"], arrays[f"env_{name}_db"] = env.times_s, env.level_db
    meta = {
        "sha256": f.sha256, "config_version": f.config_version, "sample_rate": f.sample_rate,
        "duration_s": f.duration_s, "channels": f.channels, "grid": f.grid.to_dict(),
        "loudness": {"integrated_lufs": l.integrated_lufs, "lra_lu": l.lra_lu,
                     "sample_peak_dbfs": l.sample_peak_dbfs, "true_peak_dbtp": l.true_peak_dbtp},
        "envelope_bands": {name: list(env.band) for name, env in f.envelopes.items()},
    }
    buf = io.BytesIO()
    np.savez_compressed(buf, meta=np.array(json.dumps(meta)), **arrays)
    return buf.getvalue()


def from_bytes(data: bytes) -> BaseFeatures:
    z = np.load(io.BytesIO(data))
    meta = json.loads(str(z["meta"]))
    lm = meta["loudness"]
    get = lambda k: z[k] if k in z.files else None  # noqa: E731
    return BaseFeatures(
        sha256=meta["sha256"],
        config_version=meta["config_version"],
        sample_rate=meta["sample_rate"],
        duration_s=meta["duration_s"],
        channels=meta["channels"],
        loudness=loudness.LoudnessResult(
            times_s=z["loud_times"], momentary_lufs=z["loud_m"], short_term_lufs=z["loud_s"],
            integrated_lufs=lm["integrated_lufs"], lra_lu=lm["lra_lu"],
            sample_peak_dbfs=lm["sample_peak_dbfs"], true_peak_dbtp=lm["true_peak_dbtp"],
        ),
        grid=grid.Grid.from_dict(meta["grid"]),
        spectra=bands.BandSpectra(
            bands=bands.OctaveBands(z["spec_centres"], z["spec_lower"], z["spec_upper"]),
            times_s=z["spec_times"], left=z["spec_left"], right=get("spec_right"), mid=get("spec_mid"),
            side=get("spec_side"), cross=get("spec_cross"),
        ),
        envelopes={name: bands.Envelope(z[f"env_{name}_times"], z[f"env_{name}_db"], tuple(band))
                   for name, band in meta["envelope_bands"].items()},
        onset_times_s=z["onset_times"],
        kick_onset_strength=z["kick_onset"],
        waveform=z["waveform"],
    )


def get_or_compute(audio: PreparedAudio, sha256: str, storage: Storage) -> BaseFeatures:
    path = cache_path(sha256)
    if storage.exists(path):
        return from_bytes(storage.read(path))
    features = compute(audio, sha256)
    storage.write(path, to_bytes(features), content_type="application/octet-stream")
    return features
