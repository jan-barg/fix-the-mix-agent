"""Measurement tools: run the audio code on scopes and return Evidence with charts (DESIGN.md §5.4).

These are what the Lead's tool calls execute. Every number here comes from the audio; the
models only ever see these results.
"""

import numpy as np

from app import evidence
from app.audio import bands, figures, kick, loudness, lowend, stereo, tonal
from app.audio.features import Scope, TrackData
from app.evidence import Evidence, scalar

NORMALISATION = "each track normalised once to 0 dBFS sample peak (one gain for all channels)"


def _section_shading(scopes: list[Scope]) -> dict[str, list[dict]]:
    shading: dict[str, list[dict]] = {}
    for s in scopes:
        if s.label != "Whole track":
            shading.setdefault(s.track.id, []).append({"label": s.label, "start_s": s.start_s, "end_s": s.end_s})
    return shading


# --- Loudness ---


def measure_loudness(scopes: list[Scope]) -> Evidence:
    scalars, rows, series, warnings = {}, [], [], []
    for s in scopes:
        L = s.track.features.loudness
        mask = (L.times_s > s.start_s) & (L.times_s <= s.end_s)
        st = L.short_term_lufs[mask]
        whole = s.label == "Whole track"
        integrated = L.integrated_lufs if whole else (
            loudness.integrated_from_momentary(L.momentary_lufs[mask]) if s.duration_s >= 3 else None)
        segment = s.track.audio.signal[:, s.samples()]
        sample_peak = float(bands.db(np.max(np.abs(segment)) ** 2)) if segment.size else None
        true_peak = L.max_true_peak_dbtp if whole else loudness.true_peak_dbtp(segment)
        max_st = float(np.nanmax(st)) if np.isfinite(st).any() else None
        median_st = float(np.nanmedian(st)) if np.isfinite(st).any() else None
        d = s.describe()
        scalars[f"{d} | integrated loudness"] = scalar(integrated, "LUFS")
        scalars[f"{d} | max short-term loudness"] = scalar(max_st, "LUFS")
        scalars[f"{d} | median short-term loudness"] = scalar(median_st, "LUFS")
        scalars[f"{d} | sample peak"] = scalar(sample_peak, "dBFS")
        scalars[f"{d} | true peak"] = scalar(true_peak, "dBTP")
        if whole:
            scalars[f"{d} | loudness range"] = scalar(L.lra_lu, "LU")
            if integrated is not None:
                scalars[f"{d} | peak-to-loudness ratio (true peak minus integrated)"] = scalar(true_peak - integrated, "dB")
        if integrated is None:
            warnings.append(f"{d}: integrated loudness unavailable (passage shorter than 3 s or silent)")
        rows.append({"track_id": s.track.id, "label": d, "integrated_lufs": integrated,
                     "max_short_term_lufs": max_st if max_st is not None else -70.0,
                     "true_peak_dbtp": true_peak})
        series.append({"track_id": s.track.id, "label": d, "times_s": L.times_s[mask], "short_term_lufs": st})

    # A section of a track whose whole timeline is already plotted shows as shading, not its own panel.
    whole_ids = {s.track.id for s in scopes if s.label == "Whole track"}
    series = [x for x, s in zip(series, scopes) if s.label == "Whole track" or s.track.id not in whole_ids]
    desc = evidence.describe_scopes(scopes)
    figs = [
        evidence.figure("Loudness over time", f"Source: short-term loudness of {desc}.",
                        figures.loudness_timeline(series, "Loudness over time", _section_shading(scopes))),
        evidence.figure("Loudness and peaks at equal sample peak", f"Source: loudness and peak comparison of {desc}.",
                        figures.loudness_comparison(rows, "Loudness and peaks at equal sample peak")),
    ]
    method = {"meter": "EBU R128 / ITU-R BS.1770 via FFmpeg ebur128 (100 ms steps)",
              "section integrated loudness": "BS.1770 gating over the section's 400 ms blocks",
              "true peak": "4x oversampled", "normalisation": NORMALISATION}
    return evidence.make("measure_loudness", scopes, method, {"scalars": scalars}, figs, warnings)


# --- Tonal balance ---


def measure_tonal_balance(scopes: list[Scope], mode: str = "peak_normalised",
                          combine: list[int] | None = None) -> Evidence:
    """scopes[0] is the user's passage; the rest are references. `combine` lists indices of
    reference scopes to average into a combined target (equal weights)."""
    curves = [tonal.long_term_spectrum(s) for s in scopes]
    if combine:
        names = ", ".join(scopes[i].track.name for i in combine)
        curves.append(tonal.combined_target([curves[i] for i in combine], f"Average of {names}"))
    notes, signal = [], "peak_normalised"
    if mode == "level_matched":
        curves = [curves[0]] + [tonal.level_matched(c, curves[0]) for c in curves[1:]]
        signal = "level_matched"
        notes.append("Secondary view: each comparison curve shifted by one broadband offset so its 100 Hz-10 kHz "
                     "power equals your track's. Use it for tonal shape only, not loudness.")

    scalars = {}
    for c in curves:
        for band, v in c.summary_db.items():
            scalars[f"{c.label} | {band.replace('_', '-')} band level"] = scalar(v, "dB")
        if c.offset_db:
            scalars[f"{c.label} | level-match offset applied"] = scalar(c.offset_db, "dB")
    for c in curves[1:]:
        for band in c.summary_db:
            scalars[f"{curves[0].label} minus {c.label} | {band.replace('_', '-')} band"] = \
                scalar(curves[0].summary_db[band] - c.summary_db[band], "dB")
    if combine:
        notes.append("The combined target averages the contributors' dB curves with equal weights; it is a ballpark, "
                     "not a recording.")

    title = "Tonal balance" + (" (level-matched view)" if mode == "level_matched" else "")
    desc = " vs ".join(c.label for c in curves)
    fig = figures.tonal_curves(
        [{"track_id": c.track_id, "label": c.label, "centres": c.bands.centres, "level_db": c.level_db} for c in curves],
        title, note=notes[0] if mode == "level_matched" else "")
    method = {"spectrum": "power-averaged 1/6-octave long-term spectrum of (|L|²+|R|²)/2, Hann, n_fft 16384, hop 4096",
              "level": "dB re full-scale mean square (a full-scale sine reads -3 dB)",
              "summary bands": {k: f"{lo:g}-{hi:g} Hz" for k, (lo, hi) in bands.TONAL_SUMMARY_BANDS.items()},
              "normalisation": NORMALISATION, "mode": mode}
    return evidence.make("measure_tonal_balance", scopes, method, {"scalars": scalars, "notes": notes},
                         [evidence.figure(title, f"Source: spectral comparison of {desc}.", fig)],
                         params={"mode": mode, "combine": combine}, signal=signal)


# --- Stereo ---


def measure_stereo(scopes: list[Scope]) -> Evidence:
    scalars, results, warnings = {}, [], []
    for s in scopes:
        r = stereo.analyse(s)
        d = s.describe()
        if r is None:
            warnings.append(f"{d} is mono: stereo width cannot be measured")
            scalars[f"{d} | channels"] = scalar("mono")
            continue
        results.append(r)
        for region, v in r.region_side_to_mid_db.items():
            scalars[f"{d} | side relative to mid, {region}"] = scalar(v, "dB")
        for region, v in r.region_mono_change_db.items():
            scalars[f"{d} | level change in mono, {region}"] = scalar(v, "dB")
            if v < stereo.CANCELLATION_DB:
                warnings.append(f"{d}: {region} loses {abs(v):.1f} dB in mono, more than fold-down scaling explains")
        corr = r.window_correlation[np.isfinite(r.window_correlation)]
        if corr.size:
            scalars[f"{d} | median L/R correlation"] = scalar(float(np.median(corr)))
            scalars[f"{d} | share of 400 ms windows with negative correlation"] = scalar(float(np.mean(corr < 0) * 100), "%")
    figs = []
    if results:
        desc = evidence.describe_scopes(scopes)
        figs.append(evidence.figure("Stereo width and mono change by frequency", f"Source: stereo comparison of {desc}.",
                                    figures.stereo_by_frequency([{"track_id": r.track_id, "label": r.label, "centres": r.bands.centres,
                                                                  "side_to_mid_db": r.side_to_mid_db, "mono_change_db": r.mono_change_db}
                                                                 for r in results], "Stereo width and mono change by frequency")))
        figs.append(evidence.figure("L/R correlation over time", f"Source: L/R correlation of {desc}.",
                                    figures.correlation_timeline([{"track_id": r.track_id, "label": r.label, "times_s": r.window_times_s,
                                                                   "correlation": r.window_correlation} for r in results],
                                                                 "L/R correlation over time")))
    method = {"mid/side": "mid = (L+R)/2, side = (L-R)/2", "mono change": "mid power vs average channel power, per band",
              "bands": "1/3 octave", "correlation window": "400 ms", "normalisation": NORMALISATION + "; mono fold-down not renormalised"}
    status = "unavailable" if not results else None
    return evidence.make("measure_stereo", scopes, method, {"scalars": scalars}, figs, warnings, status=status)


# --- Kick ---


def find_kick_candidates(track: TrackData, ranges: list[tuple[float, float]] | None = None, n: int = 3) -> Evidence:
    candidates = kick.find_candidates(track, ranges, n)
    scope = Scope(track, 0.0, track.audio.duration_s, "Whole track")
    scalars, table, warnings = {}, [], []
    for rank, c in enumerate(candidates, 1):
        prefix = f"{track.name} kick candidate {rank} at {int(c.time_s // 60)}:{c.time_s % 60:05.2f}"
        scalars[f"{prefix} | fullness relative to strongest kicks"] = scalar(c.fullness_db, "dB")
        scalars[f"{prefix} | isolation above preceding low end"] = scalar(c.isolation_db, "dB")
        scalars[f"{prefix} | body drop within 300 ms"] = scalar(c.decay_drop_db, "dB")
        table.append({"rank": rank, "time_s": round(c.time_s, 3), "flags": c.flags, "reasons": c.reasons})
    if not candidates:
        warnings.append("no kick candidates found")
    elif candidates[0].flags:
        warnings.append(f"best candidate is flagged: {', '.join(candidates[0].flags)}")
    figs = []
    if candidates:
        analyses = [kick.analyse(track, c.time_s, c.time_s + c.gap_s) for c in candidates]
        figs.append(evidence.figure(
            f"{track.name}: kick candidates", f"Source: the top kick candidates found in {track.name}.",
            figures.kick_spectrograms([{"track_id": track.id, "label": f"Candidate {i} at {c.time_s:.2f} s",
                                        "crop": a.crop, "sample_rate": a.sample_rate}
                                       for i, (c, a) in enumerate(zip(candidates, analyses), 1)],
                                      f"{track.name}: kick candidates")))
    method = {"onsets": "kick-weighted onset envelope (mel bands below 250 Hz)",
              "ranking": "fullness (body peak vs the track's 95th percentile) first, then isolation from preceding "
                         "low end, inspectable decay and 8-16 kHz contamination; candidates at least 4 s apart",
              "body band": "30-120 Hz"}
    return evidence.make("find_kick_candidates", [scope], method, {"scalars": scalars, "tables": {"candidates": table}},
                         figs, warnings, params={"ranges": ranges, "n": n})


def analyse_kicks(selections: list[tuple[TrackData, float, float | None]]) -> Evidence:
    """selections: (track, onset time, next onset time or None) per kick to compare."""
    analyses = [(t, kick.analyse(t, time_s, next_s)) for t, time_s, next_s in selections]
    scopes = [Scope(t, a.time_s - kick.CROP_PRE_S, a.time_s + kick.CROP_S, f"kick at {a.time_s:.2f} s") for t, a in analyses]
    scalars, notes = {}, []
    for (t, a), s in zip(analyses, scopes):
        d = s.describe()
        scalars[f"{d} | attack-to-body ratio"] = scalar(a.attack_body_db, "dB")
        scalars[f"{d} | body decay to -20 dB"] = scalar(a.decay_ms, "ms")
        scalars[f"{d} | body spectral peak"] = scalar(a.body_peak_hz, "Hz")
        scalars[f"{d} | body centroid (30-300 Hz)"] = scalar(a.body_centroid_hz, "Hz")
        scalars[f"{d} | pitch near onset"] = scalar(a.pitch_start_hz, "Hz")
        scalars[f"{d} | pitch after ~130 ms"] = scalar(a.pitch_end_hz, "Hz")
        scalars[f"{d} | peak level"] = scalar(a.peak_dbfs, "dBFS")
        scalars[f"{d} | RMS level, first 150 ms"] = scalar(a.rms_dbfs, "dBFS")
        if a.decay_note:
            notes.append(f"{d}: {a.decay_note}")
        notes.append(f"{d}: {a.pitch_note}")
    desc = evidence.describe_scopes(scopes)
    kicks = [{"track_id": t.id, "label": s.describe(), "envelope_ms": a.envelope_ms, "envelope_db": a.envelope_db,
              "crop": a.crop, "sample_rate": a.sample_rate} for (t, a), s in zip(analyses, scopes)]
    figs = [
        evidence.figure("Kick body envelopes", f"Source: kick envelope comparison of {desc}.",
                        figures.kick_envelopes(kicks, "Kick body envelopes")),
        evidence.figure("Kick spectrograms", f"Source: kick spectrogram comparison of {desc}.",
                        figures.kick_spectrograms(kicks, "Kick spectrograms (shared colour scale)")),
    ]
    method = {"crop": "10 ms before to 400 ms after the onset, mid signal, parent-track normalisation",
              "attack-to-body": "2-8 kHz mean square over 0-15 ms vs 40-120 Hz over 15-150 ms",
              "decay": "30-120 Hz analytic envelope (5 ms smoothing) falling 20 dB below its peak, before the next onset",
              "pitch": "spectral peak 35-200 Hz in 46 ms windows; reported only with a clear tonal peak",
              "normalisation": NORMALISATION}
    return evidence.make("analyse_kick", scopes, method, {"scalars": scalars, "notes": notes}, figs)


# --- Low end ---


def measure_low_end(scopes: list[Scope], band_names: list[str] | None = None) -> Evidence:
    band_names = band_names or list(bands.LOW_END_BANDS)
    results = [lowend.analyse(s) for s in scopes]
    scalars, warnings, notes = {}, [], []
    for s, r in zip(scopes, results):
        d = s.describe()
        scalars[f"{d} | kick events analysed"] = scalar(len(r.events_s))
        for name in band_names:
            rec = r.recoveries[name]
            label = f"{d} | {name.replace('_', ' ')} {rec.band_hz[0]:g}-{rec.band_hz[1]:g} Hz"
            if rec.available:
                scalars[f"{label} | dip depth"] = scalar(rec.depth_db, "dB")
                scalars[f"{label} | dip time after the kick"] = scalar(rec.dip_ms, "ms")
                scalars[f"{label} | recovery time from the dip"] = scalar(rec.recovery_ms, "ms")
                scalars[f"{label} | recovery time uncertainty (bootstrap IQR)"] = scalar(rec.recovery_iqr_ms, "ms")
                scalars[f"{label} | recovered, time after the kick"] = scalar(rec.recovered_at_ms, "ms")
                scalars[f"{label} | recovery as fraction of a beat"] = scalar(rec.recovery_beats, "beats")
                scalars[f"{label} | kicks showing the dip"] = scalar(rec.consistent_share * 100, "%")
                notes.append(f"{label}: recovery shape {rec.shape}")
            else:
                scalars[f"{label} | recovery"] = scalar(None)
                warnings.append(f"{label}: recovery unavailable: {rec.reason}")
        for c, lvl in zip(r.distribution_bands.centres, r.distribution_db):
            if c in r.distribution_bands.centres[::2]:
                scalars[f"{d} | low-end level at {c:.0f} Hz"] = scalar(float(lvl), "dB")
    notes.append("Observed recovery in the finished mix, not a compressor setting. " + lowend.CRITERION)
    smoothing = ", ".join(f"{n.replace('_', ' ')} {max(bands.ENVELOPE_WINDOW_S, 1 / bands.LOW_END_BANDS[n][0]) * 1000:.0f} ms"
                          for n in band_names)
    notes.append(f"Times are in 5 ms steps; envelope smoothing also limits timing resolution ({smoothing}).")
    notes.append("The sub band is dominated by the kick itself; upper-bass and low-mid bands carry bass harmonics "
                 "and are the better evidence of bass ducking.")
    desc = evidence.describe_scopes(scopes)
    figs = [
        evidence.figure("Low-end level", f"Source: low-end comparison of {desc}.",
                        figures.low_end_distribution([{"track_id": r.track_id, "label": r.label, "centres": r.distribution_bands.centres,
                                                       "level_db": r.distribution_db} for r in results], "Low-end level")),
        evidence.figure("Low end around the kick", f"Source: kick-aligned low-end envelopes of {desc}.",
                        figures.recovery_envelopes([{"track_id": r.track_id, "label": r.label, "bpm": r.bpm,
                                                     "recoveries": r.recoveries} for r in results], band_names,
                                                   "Low end around the kick")),
    ]
    first = scopes[0]
    passage = min(first.duration_s, 4 * 4 * first.track.features.grid.beat_s)
    panels = []
    for s in scopes:
        start = s.start_s
        seg = s.track.mid[int(start * s.track.audio.sample_rate): int((start + passage) * s.track.audio.sample_rate)]
        panels.append({"track_id": s.track.id, "label": s.describe() + f" (first {passage:.1f} s)", "signal": seg,
                       "sample_rate": s.track.audio.sample_rate, "start_s": start})
    markers = {r.track_id: [t for t in r.events_s if s.start_s <= t < s.start_s + passage] for s, r in zip(scopes, results)}
    figs.append(evidence.figure("Low-end spectrogram", f"Source: low-end spectrogram of {desc}.",
                                figures.spectrograms(panels, "low_detail", "Low-end spectrogram (kick onsets marked)", markers=markers)))
    method = {"bands": {k: f"{lo:g}-{hi:g} Hz" for k, (lo, hi) in bands.LOW_END_BANDS.items()},
              "envelope": "zero-phase 4th-order Butterworth band-pass, analytic (Hilbert) envelope averaged over "
                          "one period of the band's lowest frequency (at least 10 ms), every 5 ms",
              "kick events": "strongest kick-weighted onset within 40 ms of each grid beat",
              "recovery criterion": lowend.CRITERION, "normalisation": NORMALISATION}
    status = "qualified" if warnings else None
    return evidence.make("measure_low_end", scopes, method, {"scalars": scalars, "notes": notes}, figs, warnings,
                         status=status, params={"bands": band_names})


# --- Spectrogram ---


def render_spectrogram(scopes: list[Scope], preset: str = "overview", fmin: float = 20.0, fmax: float | None = None) -> Evidence:
    panels = [{"track_id": s.track.id, "label": s.describe(), "signal": s.track.mid[s.samples()],
               "sample_rate": s.track.audio.sample_rate, "start_s": s.start_s} for s in scopes]
    p = figures.SPECTROGRAM_PRESETS[preset]
    title = f"Spectrogram ({preset.replace('_', ' ')})"
    fig = figures.spectrograms(panels, preset, title, fmin, fmax)
    method = {"preset": preset, "n_fft": p["n_fft"], "hop": p["hop"], "window": "Hann", "scale": "dB, one shared range",
              "signal": "mid (L+R)/2", "normalisation": NORMALISATION}
    return evidence.make("render_spectrogram", scopes, method, {"scalars": {}}, [
        evidence.figure(title, f"Source: spectrogram of {evidence.describe_scopes(scopes)}.", fig)],
        params={"preset": preset, "fmin": fmin, "fmax": fmax})
