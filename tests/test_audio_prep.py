"""Synthetic-signal checks for decoding, peak normalisation and loudness metering (SPEC §12.4)."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.audio import decode, loudness

SR = 44100


def sine(freq: float, seconds: float, amplitude: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def write_wav(path: Path, signal: np.ndarray) -> Path:
    """Write a (channels, samples) float signal as a 32-bit float WAV through FFmpeg."""
    channels = signal.shape[0]
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", str(channels),
           "-i", "pipe:0", "-c:a", "pcm_f32le", str(path)]
    subprocess.run(cmd, input=np.ascontiguousarray(signal.T, dtype="<f4").tobytes(), check=True)
    return path


def test_one_gain_for_all_channels_and_channel_balance_kept():
    left, right = sine(1000, 2, 0.25), sine(1000, 2, 0.5)
    normalised, peak_db, gain_db = decode.peak_normalise(np.stack([left, right]))
    assert np.max(np.abs(normalised)) == pytest.approx(1.0, abs=1e-6)
    assert peak_db == pytest.approx(20 * np.log10(0.5), abs=1e-3)
    assert gain_db == pytest.approx(-peak_db)
    # Left stays 6 dB below right: channels are never normalised independently.
    ratio = np.max(np.abs(normalised[0])) / np.max(np.abs(normalised[1]))
    assert 20 * np.log10(ratio) == pytest.approx(-6.02, abs=0.01)


def test_overs_above_full_scale_are_normalised_down():
    normalised, peak_db, gain_db = decode.peak_normalise(sine(500, 1, 1.168)[None, :])
    assert peak_db == pytest.approx(1.35, abs=0.01)
    assert gain_db == pytest.approx(-1.35, abs=0.01)
    assert np.max(np.abs(normalised)) == pytest.approx(1.0, abs=1e-6)


def test_silence_is_not_normalised():
    signal = np.zeros((2, SR), dtype=np.float32)
    normalised, peak_db, gain_db = decode.peak_normalise(signal)
    assert peak_db is None and gain_db is None
    assert not np.any(normalised)


def test_full_scale_stereo_1k_sine_reads_about_0_lufs():
    tone = sine(997, 10, 1.0)
    result = loudness.measure(np.stack([tone, tone]), SR)
    # BS.1770: a 0 dBFS 1 kHz sine reads -3.01 LUFS per channel; two identical channels add 3 dB.
    assert result.integrated_lufs == pytest.approx(0.0, abs=0.2)
    assert result.max_sample_peak_dbfs == pytest.approx(0.0, abs=0.01)
    assert result.max_true_peak_dbtp >= result.max_sample_peak_dbfs - 0.01


def test_constant_gain_change_gives_identical_analysis(tmp_path):
    rng = np.random.default_rng(0)
    music = (rng.standard_normal((2, SR * 8)) * 0.1).astype(np.float32)
    loud = decode.prepare(write_wav(tmp_path / "loud.wav", music), SR)
    quiet = decode.prepare(write_wav(tmp_path / "quiet.wav", music * np.float32(0.5)), SR)
    assert loud.applied_gain_db == pytest.approx(quiet.applied_gain_db - 6.02, abs=0.01)
    a, b = loudness.measure(loud.signal, SR), loudness.measure(quiet.signal, SR)
    assert a.integrated_lufs == pytest.approx(b.integrated_lufs, abs=0.01)
    np.testing.assert_allclose(a.short_term_lufs, b.short_term_lufs, atol=0.01, equal_nan=True)


def test_section_contrast_survives_normalisation():
    quiet_build = sine(200, 6, 0.1)
    loud_drop = sine(200, 6, 0.8)
    track = np.concatenate([quiet_build, loud_drop])[None, :]
    normalised, _, _ = decode.peak_normalise(track)
    result = loudness.measure(normalised, SR)
    build = np.nanmean(result.short_term_lufs[(result.times_s > 3.5) & (result.times_s <= 6)])
    drop = np.nanmean(result.short_term_lufs[(result.times_s > 9.5) & (result.times_s <= 12)])
    assert drop - build == pytest.approx(18.06, abs=0.2)  # 20*log10(0.8/0.1)


def test_mono_stays_mono(tmp_path):
    prepared = decode.prepare(write_wav(tmp_path / "mono.wav", sine(440, 3, 0.3)[None, :]), SR)
    assert prepared.channels == 1
    assert prepared.applied_gain_db == pytest.approx(10.46, abs=0.01)


def test_too_short_is_rejected(tmp_path):
    path = write_wav(tmp_path / "short.wav", sine(440, 2, 0.3)[None, :])
    with pytest.raises(decode.AudioError, match="must be between"):
        decode.prepare(path, SR, min_duration_s=20)


def test_not_audio_is_rejected(tmp_path):
    path = tmp_path / "notes.mp3"
    path.write_text("this is not audio")
    with pytest.raises(decode.AudioError):
        decode.prepare(path, SR)
