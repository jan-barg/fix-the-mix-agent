"""Synthetic checks for the measurement tools (DESIGN.md §12): signals with known answers.

The ducking and kick tests build full base features (grid, onsets, envelopes) from a synthetic
128 BPM loop, so they also exercise beat tracking and kick-event detection.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from app.audio import bands, features, grid, kick, loudness, lowend, sections, stereo
from app.audio.decode import PreparedAudio, ProbeInfo
from app.audio.features import Scope, TrackData

SR = 44100
BPM = 128.0
BEAT = 60.0 / BPM
N_BEATS = 64


def prepared(signal: np.ndarray) -> PreparedAudio:
    signal = np.atleast_2d(signal).astype(np.float32)
    signal = signal / np.max(np.abs(signal))  # peak-normalised, like every analysed track
    probe = ProbeInfo("synthetic", "pcm_f32le", SR, signal.shape[0], signal.shape[1] / SR, None)
    return PreparedAudio(signal, SR, probe, 0.0, 0.0, False, None)


def full_track(signal: np.ndarray) -> TrackData:
    audio = prepared(signal)
    return TrackData("user", "Synthetic", "user", audio, features.compute(audio, "synthetic"))


def spectra_only_track(signal: np.ndarray) -> TrackData:
    """Enough of a track for the stereo measurements, without beat tracking on noise."""
    audio = prepared(signal)
    return TrackData("user", "Synthetic", "user", audio, SimpleNamespace(spectra=bands.band_spectra(audio.signal, SR)))


def whole(track: TrackData) -> Scope:
    return Scope(track, 0.0, track.audio.duration_s, "Whole track")


# --- Synthetic loop: kicks on every beat, a bass line under them ---


def kick_hit(seconds: float = 0.4) -> np.ndarray:
    """Pitch-swept sine kick, 70 Hz falling to 45 Hz, kept below the upper-bass band."""
    t = np.arange(int(seconds * SR)) / SR
    freq = 45.0 + 25.0 * np.exp(-t / 0.03)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    return np.sin(phase) * np.exp(-t / 0.1)


def loop(bass_hz: float, bass_amp: float, gain_db) -> np.ndarray:
    """N_BEATS of kicks plus a bass sine whose level follows gain_db(seconds since the kick)."""
    n = int(N_BEATS * BEAT * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    hit = kick_hit()
    for b in range(N_BEATS):
        i = int(round(b * BEAT * SR))
        seg = hit[: n - i]
        out[i:i + len(seg)] += seg
    since_kick = t % BEAT
    out += bass_amp * 10 ** (gain_db(since_kick) / 20) * np.sin(2 * np.pi * bass_hz * t)
    return out


HOLD_S, RELEASE_S, DEPTH_DB = 0.050, 0.150, 12.0


def sidechain(since_kick: np.ndarray) -> np.ndarray:
    """-12 dB held for 50 ms, then a straight line in dB back to 0 dB at 200 ms."""
    ramp = np.clip((since_kick - HOLD_S) / RELEASE_S, 0.0, 1.0)
    return -DEPTH_DB * (1.0 - ramp)


@pytest.fixture(scope="module")
def ducked_track() -> TrackData:
    return full_track(loop(220.0, 0.3, sidechain))


# --- Band spectra ---


def test_band_powers_add_up_to_mean_square():
    t = np.arange(4 * SR) / SR
    x = (np.sin(2 * np.pi * 1000 * t) + 0.5 * np.sin(2 * np.pi * 80 * t)).astype(np.float32)
    spectra = bands.band_spectra(x[None, :], SR)
    total = spectra.left[:, spectra.frames_in(1.0, 3.0)].sum(axis=0).mean()
    assert bands.db(total) == pytest.approx(bands.db(0.5 + 0.125), abs=0.1)


def test_full_scale_sine_reads_minus_3_db_in_its_band():
    t = np.arange(4 * SR) / SR
    spectra = bands.band_spectra(np.sin(2 * np.pi * 1000 * t).astype(np.float32)[None, :], SR)
    power = spectra.left[:, spectra.frames_in(1.0, 3.0)].mean(axis=1)
    i = int(np.argmin(np.abs(spectra.bands.centres - 1000)))
    assert bands.db(power[i]) == pytest.approx(-3.01, abs=0.1)
    assert power[i] / power.sum() > 0.99


# --- Loudness ---


def test_section_integrated_loudness_matches_ffmpeg():
    rng = np.random.default_rng(2)
    levels = [(8, 0.1), (8, 0.03), (2, 0.0), (6, 0.2)]  # the 0.03 part falls under the relative gate
    x = np.concatenate([amp * rng.standard_normal((2, int(s * SR))) for s, amp in levels], axis=1)
    result = loudness.measure(x.astype(np.float32), SR)
    assert loudness.integrated_from_momentary(result.momentary_lufs) == pytest.approx(result.integrated_lufs, abs=0.2)


# --- Stereo ---


def stereo_noise(right_from_left) -> stereo.StereoResult:
    rng = np.random.default_rng(3)
    left = 0.3 * rng.standard_normal(6 * SR)
    right = right_from_left(left, rng)
    return stereo.analyse(whole(spectra_only_track(np.stack([left, right]))))


def test_identical_channels_are_mono():
    r = stereo_noise(lambda left, rng: left.copy())
    valid = ~np.isnan(r.mono_change_db)
    assert np.allclose(r.mono_change_db[valid], 0.0, atol=0.05)
    assert np.all(r.side_to_mid_db[valid] <= -59.0)  # clamped floor: no side at all
    assert np.allclose(r.correlation[valid], 1.0, atol=1e-3)
    assert np.allclose(r.window_correlation, 1.0, atol=1e-6)


def test_opposite_polarity_cancels_in_mono():
    r = stereo_noise(lambda left, rng: -left)
    valid = ~np.isnan(r.mono_change_db)
    assert np.all(r.mono_change_db[valid] < stereo.CANCELLATION_DB)
    assert np.allclose(r.correlation[valid], -1.0, atol=1e-3)
    assert np.allclose(r.window_correlation, -1.0, atol=1e-6)


def test_unrelated_channels_lose_3_db_in_mono():
    r = stereo_noise(lambda left, rng: 0.3 * rng.standard_normal(len(left)))
    mid_bands = (r.bands.centres >= 200) & (r.bands.centres <= 10000)
    assert np.allclose(r.mono_change_db[mid_bands], -3.01, atol=0.6)
    assert np.allclose(r.side_to_mid_db[mid_bands], 0.0, atol=1.2)
    assert np.all(np.abs(r.window_correlation) < 0.1)


# --- Kick events and ducking recovery ---


def test_kick_events_land_on_the_kicks(ducked_track):
    assert ducked_track.features.grid.bpm == pytest.approx(BPM, abs=0.01)
    events = lowend.kick_events(ducked_track, 2 * BEAT, (N_BEATS - 2) * BEAT)
    true_kicks = np.arange(N_BEATS) * BEAT
    errors = np.array([np.min(np.abs(true_kicks - e)) for e in events])
    assert len(events) >= N_BEATS - 6
    assert np.median(errors) < 0.012


def test_known_sidechain_depth_and_recovery(ducked_track):
    result = lowend.analyse(whole(ducked_track)).recoveries["upper_bass"]
    assert result.available, result.reason
    assert result.depth_db == pytest.approx(DEPTH_DB, abs=1.5)
    # Back within 10% of the depth: 50 ms hold + 90% of the 150 ms ramp = 185 ms after the kick.
    assert result.recovered_at_ms == pytest.approx(1000 * (HOLD_S + 0.9 * RELEASE_S), abs=20)
    assert result.consistent_share > 0.9


def test_unducked_bass_reports_no_dip():
    track = full_track(loop(220.0, 0.3, lambda s: np.zeros_like(s)))
    result = lowend.analyse(whole(track)).recoveries["upper_bass"]
    assert not result.available
    assert "no clear dip" in result.reason


def test_bass_changing_inside_the_baseline_window_is_unavailable():
    def phrase(since_kick):
        # Ducked as before, but the bass drops 10 dB from 65% of the beat: a moving bass line.
        return sidechain(since_kick) - 10.0 * (since_kick >= 0.65 * BEAT)

    track = full_track(loop(220.0, 0.3, phrase))
    result = lowend.analyse(whole(track)).recoveries["upper_bass"]
    assert not result.available
    assert "no defensible baseline" in result.reason


# --- Kick ranking ---


def test_exposed_kick_outranks_one_over_a_bass():
    # A 50 Hz bass sits under the first half of the loop only.
    first_half = lambda s: np.zeros_like(s)  # noqa: E731
    x = loop(50.0, 0.5, first_half)
    x[int(N_BEATS / 2 * BEAT * SR):] -= (0.5 * np.sin(2 * np.pi * 50.0 * np.arange(len(x)) / SR))[int(N_BEATS / 2 * BEAT * SR):]
    track = full_track(x)
    candidates = kick.find_candidates(track, n=5)
    assert candidates
    best = candidates[0]
    assert best.time_s >= N_BEATS / 2 * BEAT - 0.05, best
    exposed = [c for c in candidates if c.time_s >= N_BEATS / 2 * BEAT - 0.05]
    over_bass = [c for c in candidates if c.time_s < N_BEATS / 2 * BEAT - 0.05]
    assert over_bass, "expected some candidates over the bass to compare against"
    assert max(c.score for c in over_bass) < min(c.score for c in exposed)
    assert max(c.isolation_db for c in over_bass) < min(c.isolation_db for c in exposed) - 6
    # Fullness is judged against exposed kicks, so the clean ones are not called weaker.
    assert all(c.fullness_db > -1.5 and not any("bass overlap" in f for f in c.flags) for c in exposed)


# --- Grid: files that start on bar 1 ---


@pytest.mark.parametrize("cut_ms", [0, 10, 20])
def test_file_starting_on_a_kick_keeps_bar_1_at_the_start(cut_ms):
    # Cutting into the first kick moves the fitted first beat from just after 0 s to just before it.
    # Either way it is the file's first beat, and with no bar structure to say otherwise, bar 1.
    x = loop(220.0, 0.3, sidechain)[int(cut_ms / 1000 * SR):]
    g = grid.analyse((x / np.max(np.abs(x))).astype(np.float32), SR)
    assert abs(g.beats_s[0]) <= grid.START_TOLERANCE_S
    assert g.downbeats_s[0] == g.beats_s[0]
    track = SimpleNamespace(features=SimpleNamespace(grid=g), audio=SimpleNamespace(duration_s=len(x) / SR))
    starts = sections.bar_starts(track)
    assert starts[0] == g.beats_s[0]
    assert sections.bar_span(0.0, 4 * BEAT, starts[0], 4 * BEAT) == (1, 2)



def test_short_lead_in_is_bar_1_but_a_real_pickup_is_bar_0():
    bar = 4 * BEAT
    assert sections.bar_span(0.0, bar + 0.04, 0.04, bar) == (1, 2)  # 40 ms before the first downbeat
    assert sections.bar_span(0.0, bar + 0.3, 0.3, bar) == (0, 2)  # a 300 ms pickup
