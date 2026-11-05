"""Synthetic checks for the measurement tools (DESIGN.md §12): signals with known answers.

The ducking and kick tests build full base features (grid, onsets, envelopes) from a synthetic
128 BPM loop, so they also exercise beat tracking and kick-event detection.
"""

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app import evidence, tools
from app.audio import bands, decode, features, grid, kick, loudness, lowend, sections, stereo, tonal
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


def metered_track(signal: np.ndarray, track_id: str = "user", name: str = "Your track") -> TrackData:
    """Enough of a track for the loudness tool: prepared audio, its loudness series, a placeholder grid."""
    audio = prepared(signal)
    feats = SimpleNamespace(loudness=loudness.measure(audio.signal, SR), sha256=f"synthetic-{track_id}",
                            grid=grid.fallback(audio.duration_s))
    return TrackData(track_id, name, "user" if track_id == "user" else "reference", audio, feats)


def whole(track: TrackData) -> Scope:
    return features.whole_track(track)


def tone(freq: float, seconds: float, amplitude: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return amplitude * np.sin(2 * np.pi * freq * t)


def clicks(times_s: np.ndarray, seconds: float) -> np.ndarray:
    """Kick hits at the given times, peak-normalised."""
    n = int(seconds * SR)
    out = np.zeros(n)
    hit = kick_hit()
    for t in times_s:
        i = int(round(t * SR))
        out[i:i + len(hit[: n - i])] += hit[: n - i]
    return (out / np.max(np.abs(out))).astype(np.float32)


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
    assert total == pytest.approx(0.5 + 0.125, rel=0.01)  # mean square of the two sines


def test_full_scale_sine_reads_minus_3_db_in_its_band():
    t = np.arange(4 * SR) / SR
    spectra = bands.band_spectra(np.sin(2 * np.pi * 1000 * t).astype(np.float32)[None, :], SR)
    power = spectra.left[:, spectra.frames_in(1.0, 3.0)].mean(axis=1)
    i = int(np.argmin(np.abs(spectra.bands.centres - 1000)))
    assert bands.db(power[i]) == pytest.approx(-3.01, abs=0.1)
    assert power[i] / power.sum() > 0.99


# --- Loudness ---


def test_section_spectrum_does_not_leak_from_the_previous_section():
    # A full-scale 1 kHz tone, then a quiet 100 Hz tone. The second section contains no 1 kHz at all.
    x = np.concatenate([tone(1000, 3, 1.0), tone(100, 3, 0.01)])
    track = spectra_only_track(x[None, :])
    curve = tonal.long_term_spectrum(Scope(track, 3.0, 6.0, "outro"))
    centres = track.features.spectra.bands.centres
    assert curve.level_db[np.argmin(np.abs(centres - 1000))] < -80
    assert curve.level_db[np.argmin(np.abs(centres - 100))] == pytest.approx(-43.01, abs=0.3)  # 0.01 amplitude
    assert not track.features.spectra.frames_in(3.0, 3.3).any()  # shorter than one 372 ms window


def test_section_integrated_loudness_matches_ffmpeg():
    rng = np.random.default_rng(2)
    levels = [(8, 0.1), (8, 0.03), (2, 0.0), (6, 0.2)]  # the 0.03 part falls under the relative gate
    x = np.concatenate([amp * rng.standard_normal((2, int(s * SR))) for s, amp in levels], axis=1)
    result = loudness.measure(x.astype(np.float32), SR)
    assert loudness.integrated_from_momentary(result.momentary_lufs) == pytest.approx(result.integrated_lufs, abs=0.2)


def test_section_loudness_is_the_section_alone():
    # A loud passage, then a quiet outro 28 dB down. The outro's readings must not include the drop.
    rng = np.random.default_rng(4)
    x = np.concatenate([0.5 * rng.standard_normal((2, 10 * SR)), 0.02 * rng.standard_normal((2, 10 * SR))], axis=1)
    track = metered_track(x)
    outro = Scope(track, 10.0, 20.0, "outro")
    scalars = tools.measure_loudness([outro]).results["scalars"]
    alone = loudness.measure(track.audio.signal[:, 10 * SR:], SR)  # the outro metered on its own
    d = outro.describe()
    assert scalars[f"{d} | integrated loudness"]["value"] == pytest.approx(alone.integrated_lufs, abs=0.3)
    assert scalars[f"{d} | max short-term loudness"]["value"] == pytest.approx(np.nanmax(alone.short_term_lufs), abs=0.3)


def test_loudness_differences_are_measured():
    def build_and_drop(build_amp: float) -> np.ndarray:
        return np.stack(2 * [np.concatenate([tone(200, 8, build_amp), tone(200, 8, 1.0)])])

    user = metered_track(build_and_drop(0.25))
    ref = metered_track(build_and_drop(0.5), "ref1", "Reference 1")
    ub, ud = Scope(user, 0.0, 8.0, "build"), Scope(user, 8.0, 16.0, "drop")
    rb, rd = Scope(ref, 0.0, 8.0, "build"), Scope(ref, 8.0, 16.0, "drop")
    scalars = tools.measure_loudness([ub, ud, rb, rd]).results["scalars"]
    key = "integrated loudness"
    # Both drops peak at 0 dBFS after normalisation; the user's build is 12.04 dB under its drop,
    # the reference's 6.02 dB.
    assert scalars[f"{ud.describe()} minus {ub.describe()} | {key}"]["value"] == pytest.approx(12.04, abs=0.2)
    assert scalars[f"{ub.describe()} minus {rb.describe()} | {key}"]["value"] == pytest.approx(-6.02, abs=0.2)
    jump = f"({ud.describe()} minus {ub.describe()}) minus ({rd.describe()} minus {rb.describe()}) | {key}"
    assert scalars[jump]["value"] == pytest.approx(6.02, abs=0.2)


def test_two_sections_with_one_label_keep_separate_results():
    rng = np.random.default_rng(5)
    track = metered_track(np.concatenate([0.1 * rng.standard_normal((2, 8 * SR)), 0.5 * rng.standard_normal((2, 8 * SR))], axis=1))
    scalars = tools.measure_loudness([Scope(track, 0.0, 8.0, "drop"), Scope(track, 8.0, 16.0, "drop")]).results["scalars"]
    own = [k for k in scalars if k.endswith("| integrated loudness") and "minus" not in k]
    assert len(own) == 2
    assert abs(scalars[own[0]]["value"] - scalars[own[1]]["value"]) > 10


def test_silent_passage_reads_unavailable_not_a_number():
    rng = np.random.default_rng(6)
    noise = lambda: 0.3 * rng.standard_normal((2, 6 * SR))  # noqa: E731
    track = metered_track(np.concatenate([noise(), np.zeros((2, 6 * SR)), noise()], axis=1))
    gap = Scope(track, 6.0, 12.0, "break")
    ev = tools.measure_loudness([gap])
    d = gap.describe()
    for name in ("integrated loudness", "max short-term loudness", "sample peak", "true peak"):
        assert ev.results["scalars"][f"{d} | {name}"]["value"] is None, name
    assert len(ev.figures) == 2 and all(f.png for f in ev.figures)


# --- Stereo ---


def stereo_noise(right_from_left) -> stereo.StereoResult:
    rng = np.random.default_rng(3)
    left = 0.3 * rng.standard_normal(6 * SR)
    right = right_from_left(left, rng)
    return stereo.analyse(whole(spectra_only_track(np.stack([left, right]))))


def assert_covered(r: stereo.StereoResult) -> np.ndarray:
    """White noise has energy in every band from 50 Hz to 16 kHz: all of them must have readings."""
    valid = ~np.isnan(r.mono_change_db)
    audible = (r.bands.centres >= 50) & (r.bands.centres <= 16000)
    assert valid[audible].all()
    assert len(r.window_correlation) >= 10  # 6 s in 400 ms windows
    return valid


def test_identical_channels_are_mono():
    r = stereo_noise(lambda left, rng: left.copy())
    valid = assert_covered(r)
    assert np.allclose(r.mono_change_db[valid], 0.0, atol=0.05)
    assert np.all(r.side_to_mid_db[valid] <= -59.0)  # clamped floor: no side at all
    assert np.allclose(r.correlation[valid], 1.0, atol=1e-3)
    assert np.allclose(r.window_correlation, 1.0, atol=1e-6)


def test_opposite_polarity_cancels_in_mono():
    r = stereo_noise(lambda left, rng: -left)
    valid = assert_covered(r)
    assert np.all(r.mono_change_db[valid] <= -59.0)  # full cancellation, at the -60 dB reporting floor
    assert np.allclose(r.correlation[valid], -1.0, atol=1e-3)
    assert np.allclose(r.window_correlation, -1.0, atol=1e-6)


def test_unrelated_channels_lose_3_db_in_mono():
    r = stereo_noise(lambda left, rng: 0.3 * rng.standard_normal(len(left)))
    assert_covered(r)
    mid_bands = (r.bands.centres >= 200) & (r.bands.centres <= 10000)
    assert np.allclose(r.mono_change_db[mid_bands], -3.01, atol=0.6)
    assert np.allclose(r.side_to_mid_db[mid_bands], 0.0, atol=1.2)
    assert np.all(np.abs(r.window_correlation) < 0.1)


# --- Kick events and ducking recovery ---


def test_kick_events_land_on_the_kicks(ducked_track):
    assert ducked_track.features.grid.bpm == pytest.approx(BPM, abs=0.01)
    events = lowend.kick_events(ducked_track, 2 * BEAT, (N_BEATS - 2) * BEAT)
    true_kicks = np.arange(2, N_BEATS - 2) * BEAT
    nearest = np.array([np.min(np.abs(events - k)) for k in true_kicks])
    assert np.mean(nearest < 0.015) >= 0.95  # nearly every kick is found
    assert len(events) <= len(true_kicks)  # and nothing else
    assert np.percentile(nearest, 90) < 0.015


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


def test_kicks_near_the_end_of_a_passage_are_not_measured_past_it(ducked_track):
    end = 20 * BEAT + 0.1
    events = lowend.analyse(Scope(ducked_track, 0.0, end, "intro")).events_s
    # Each kick is read for one beat: kicks 1-19 fit; kick 0 starts too early for its 20 ms lead, kick 20 runs past the end.
    assert len(events) >= 17
    assert events.min() >= 0.02 and events.max() + BEAT <= end


def test_track_without_kick_onsets_has_no_kick_events():
    times = np.arange(0.0, 32 * BEAT, 128 / 22050)
    track = SimpleNamespace(features=SimpleNamespace(onset_times_s=times, kick_onset_strength=np.zeros_like(times),
                                                     grid=SimpleNamespace(beats_s=np.arange(32) * BEAT)))
    assert lowend.kick_events(track, 0.0, 32 * BEAT).size == 0


def test_noise_without_kicks_reports_no_ducking():
    track = full_track((0.3 * np.random.default_rng(7).standard_normal(30 * SR)).astype(np.float32))
    result = lowend.analyse(whole(track))
    assert not any(r.available for r in result.recoveries.values())


# --- Kick ranking ---


def test_exposed_kick_outranks_one_over_a_bass():
    # A 50 Hz bass sits under the first half of the loop only.
    first_half = lambda s: np.zeros_like(s)  # noqa: E731
    x = loop(50.0, 0.5, first_half)
    x[int(N_BEATS / 2 * BEAT * SR):] -= (0.5 * np.sin(2 * np.pi * 50.0 * np.arange(len(x)) / SR))[int(N_BEATS / 2 * BEAT * SR):]
    track = full_track(x)
    candidates = kick.find_candidates(track, n=5)
    assert candidates
    assert all(c.time_s >= 0.09 for c in candidates)  # the first kick has no low end before it to compare
    half = N_BEATS / 2 * BEAT
    best = candidates[0]
    assert best.time_s >= half - 0.05, best
    # The kick on the boundary has the bass right before it, so it belongs to neither group.
    exposed = [c for c in candidates if c.time_s >= half + BEAT / 2]
    over_bass = [c for c in candidates if c.time_s < half - BEAT / 2]
    assert exposed
    assert over_bass, "expected some candidates over the bass to compare against"
    assert max(c.score for c in over_bass) < min(c.score for c in exposed)
    assert max(c.isolation_db for c in over_bass) < min(c.isolation_db for c in exposed) - 6
    # Fullness is judged against exposed kicks, so the clean ones are not called weaker.
    assert all(c.fullness_db > -1.5 and not any("bass overlap" in f for f in c.flags) for c in exposed)


def test_kick_body_band_reads_flat_across_30_to_120_hz():
    # Equal-level tone bursts at 45, 60 and 90 Hz. (Adding separate 30-60 and 60-120 Hz envelopes
    # read 60 Hz 3 dB low.)
    x = np.concatenate([np.concatenate([tone(f, 0.5, 0.5), np.zeros(SR // 2)]) for f in (45, 60, 90)])
    env = kick._body_envelope(TrackData("user", "Synthetic", "user", prepared(x), None))
    levels = [env.level_db[(env.times_s > i + 0.1) & (env.times_s < i + 0.4)].mean() for i in range(3)]
    assert max(levels) - min(levels) < 0.5
    assert levels[0] == pytest.approx(-3.01, abs=0.3)  # full-scale sine after normalisation


# --- Grid: tempo range and fallbacks ---


@pytest.mark.parametrize("bpm, reported", [(92.0, 184.0), (96.0, 96.0), (186.0, 186.0)])
def test_tempo_is_reported_within_one_octave(bpm, reported):
    g = grid.analyse(clicks(np.arange(0, 40, 60 / bpm), 40), SR)
    assert g.bpm == pytest.approx(reported, abs=0.05)


@pytest.mark.parametrize("bpm, reported", [(128.06, 128.0), (127.5, 127.5)])
def test_near_whole_tempo_is_the_whole_number(bpm, reported):
    # 200 s: long enough that 0.06 BPM drifts the grid 94 ms, so the onsets alone prefer 128.06.
    # Within 0.1 BPM of a whole number the residue is treated as onset-timing bias (DAW tempos are
    # whole numbers); a half-BPM tempo is a real setting and is kept.
    g = grid.analyse(clicks(np.arange(0, 200, 60 / bpm), 200), SR)
    assert g.bpm == pytest.approx(reported, abs=0.01)


DEMO = Path(__file__).resolve().parent.parent / "demo" / "audio" / "scars-extended.mp3"


@pytest.mark.skipif(not DEMO.exists(), reason="demo audio is not in git")
def test_scars_from_bar_33_reads_134_and_lands_on_the_bar():
    # The excerpt has no clean intro; its dense drops pulled the fit to 134.07 and the grid ~100 ms late.
    audio = decode.prepare(DEMO, SR)
    mono = features.mid_signal(audio.signal)
    bar_33 = grid.analyse(mono, SR).downbeats_s[32]
    g = grid.analyse(mono[int(round(bar_33 * SR)):], SR)
    assert g.bpm == 134.0
    assert g.downbeats_s[0] == pytest.approx(0.0, abs=0.015)


def test_short_track_tempo_check_compares_more_than_one_window():
    # 40 s: the second half restarts half a beat late. With one window for the whole track the
    # deviation was zero by construction.
    times = np.concatenate([np.arange(0, 20, BEAT), 20 + BEAT / 2 + np.arange(0, 20 - BEAT, BEAT)])
    g = grid.analyse(clicks(times, 40), SR)
    assert g.beat_residual_s > 0.005


def test_no_beat_gives_a_placeholder_grid_that_survives_the_cache(monkeypatch):
    def no_beat(*_):
        raise ValueError("Too few beats detected to build a grid.")

    monkeypatch.setattr(grid, "analyse", no_beat)
    feats = features.compute(prepared(tone(220, 25, 0.5)), "pad")
    assert feats.grid.downbeat_confidence == "none"
    assert feats.grid.bpm == 120.0 and feats.grid.beats_s[0] == 0.0
    json.dumps(feats.grid.to_dict(), allow_nan=False)  # NaN residual is stored as null
    back = features.from_bytes(features.to_bytes(feats))
    assert back.grid.downbeat_confidence == "none" and np.isnan(back.grid.beat_residual_s)


def test_user_grid_keeps_the_beats_before_the_first_downbeat():
    # The user puts a downbeat 4 bars and 2 beats in; bars repeat, so the first downbeat is at 2 beats.
    g = grid.user_grid(BPM, 4 * 4 * BEAT + 2 * BEAT, 30.0)
    assert g.beats_s[0] == pytest.approx(0.0, abs=1e-9)
    assert g.downbeats_s[0] == pytest.approx(2 * BEAT)
    assert g.downbeat_phase == 2


def test_results_are_keyed_on_the_grid_and_the_next_onset(ducked_track):
    corrected = TrackData("user", "Synthetic", "user", ducked_track.audio,
                          dataclasses.replace(ducked_track.features, grid=grid.user_grid(BPM, 0.1, ducked_track.audio.duration_s)))
    assert evidence.cache_key("measure_low_end", [whole(ducked_track)], {}) != \
        evidence.cache_key("measure_low_end", [whole(corrected)], {})
    t = 8 * BEAT + 0.011
    a = tools.analyse_kicks([(ducked_track, t, None)])
    b = tools.analyse_kicks([(ducked_track, t, t + 0.2)])
    assert a.repro["cache_key"] != b.repro["cache_key"]


# --- Grid: files that start on bar 1 ---


@pytest.mark.parametrize("cut_ms", [0, 10, 20])
def test_file_starting_on_a_kick_keeps_bar_1_at_the_start(cut_ms):
    # Cutting into the first kick moves the fitted first beat from just after 0 s to just before it.
    # Either way it is the file's first beat, and with no bar structure to say otherwise, bar 1.
    x = loop(220.0, 0.3, sidechain)[int(cut_ms / 1000 * SR):]
    g = grid.analyse((x / np.max(np.abs(x))).astype(np.float32), SR)
    assert g.beats_s[0] == pytest.approx(-cut_ms / 1000, abs=0.02)  # on the (cut) first kick
    assert abs(g.beats_s[0]) <= 0.050  # the agreed tolerance for a first beat just before 0 s
    assert g.downbeats_s[0] == g.beats_s[0]
    track = SimpleNamespace(features=SimpleNamespace(grid=g), audio=SimpleNamespace(duration_s=len(x) / SR))
    starts = sections.bar_starts(track)
    assert starts[0] == g.beats_s[0]
    assert sections.bar_span(0.0, 4 * BEAT, starts[0], 4 * BEAT) == (1, 2)



def test_short_lead_in_is_bar_1_but_a_real_pickup_is_bar_0():
    bar = 4 * BEAT
    assert sections.bar_span(0.0, bar + 0.04, 0.04, bar) == (1, 2)  # 40 ms before the first downbeat
    assert sections.bar_span(0.0, bar + 0.3, 0.3, bar) == (0, 2)  # a 300 ms pickup
