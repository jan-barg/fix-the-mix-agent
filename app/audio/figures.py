"""Every chart, drawn only from measured arrays (SPEC §8.2).

Track colours are fixed for the whole report: your track, References 1-3 and a combined target.
Comparisons share axes and colour scales so equal levels look equal.
"""

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from app.audio import bands  # noqa: E402

TRACK_COLOURS = {"user": "#d1495b", "ref1": "#2e86ab", "ref2": "#3a9d5d", "ref3": "#8e6fc1", "combined": "#4d4d4d"}
SECTION_COLOURS = {"intro": "#e8eef4", "build": "#fdf0d5", "drop": "#fde2e2", "breakdown": "#e3f2e9",
                   "outro": "#ece8f4", "unknown": "#f2f2f2"}
WIDTH_IN, DPI = 11.0, 130  # 1430 px wide

plt.rcParams.update({
    "font.size": 10, "axes.titlesize": 12, "axes.titleweight": "bold", "axes.grid": True,
    "grid.alpha": 0.3, "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
    "figure.facecolor": "white", "axes.facecolor": "white",
})


def colour(track_id: str) -> str:
    return TRACK_COLOURS.get(track_id, "#999999")


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _hz(ax):
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"))
    ax.set_xlabel("Frequency (Hz)")


def _mmss(ax):
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v // 60)}:{int(v % 60):02d}"))
    ax.set_xlabel("Time (m:ss)")


# --- Loudness ---


def loudness_timeline(series: list[dict], title: str, sections: dict[str, list[dict]] | None = None) -> bytes:
    """series: [{track_id, label, times_s, short_term_lufs}]; sections: track_id -> [{label, start_s, end_s}]."""
    rows = len(series)
    fig, axes = plt.subplots(rows, 1, figsize=(WIDTH_IN, 1.9 * rows + 0.6), sharey=True, squeeze=False)
    finite = np.concatenate([s["short_term_lufs"][np.isfinite(s["short_term_lufs"])] for s in series])
    lo, hi = (np.floor(finite.min() / 3) * 3 - 1, np.ceil(finite.max() / 3) * 3 + 1) if finite.size else (-40, 0)
    for ax, s in zip(axes[:, 0], series):
        for sec in (sections or {}).get(s["track_id"], []):
            ax.axvspan(sec["start_s"], sec["end_s"], color=SECTION_COLOURS.get(sec["label"].split()[0].lower(), "#f2f2f2"), lw=0)
            ax.text((sec["start_s"] + sec["end_s"]) / 2, hi - 0.5, sec["label"], ha="center", va="top", fontsize=8, color="#555")
        ax.plot(s["times_s"], s["short_term_lufs"], color=colour(s["track_id"]), lw=1.2)
        ax.set_ylim(lo, hi)
        ax.set_ylabel("LUFS (short-term)")
        ax.set_title(s["label"], fontsize=10, loc="left")
        _mmss(ax)
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    fig.text(0.01, -0.01, "Short-term loudness (3 s window). Each track normalised once to 0 dBFS sample peak.",
             fontsize=8, color="#555")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return _png(fig)


def loudness_comparison(rows: list[dict], title: str) -> bytes:
    """rows: [{track_id, label, integrated_lufs, max_short_term_lufs, true_peak_dbtp}]; any value may be
    None (a passage too short or silent to meter), and is then left out rather than drawn."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(WIDTH_IN, 0.42 * len(rows) + 1.6), sharey=True,
                                   gridspec_kw={"width_ratios": [2, 1]})
    y = np.arange(len(rows))[::-1]  # first row at the top
    for i, r in zip(y, rows):
        c = colour(r["track_id"])
        st, integrated, tp = r["max_short_term_lufs"], r["integrated_lufs"], r["true_peak_dbtp"]
        if integrated is not None and st is not None:
            ax1.plot([integrated, st], [i, i], color=c, lw=1.2, alpha=0.6)
        if integrated is not None:
            ax1.plot(integrated, i, "o", color=c, ms=8)
            ax1.annotate(f"{integrated:.1f}", (integrated, i), xytext=(-6, 0),
                         textcoords="offset points", ha="right", va="center", fontsize=8)
        if st is not None:
            ax1.plot(st, i, "o", mfc="white", mec=c, mew=1.6, ms=8)
            ax1.annotate(f"{st:.1f}", (st, i), xytext=(6, 0), textcoords="offset points", va="center", fontsize=8)
        if tp is not None and np.isfinite(tp):
            ax2.plot([0, tp], [i, i], color=c, lw=3)
            ax2.annotate(f"{tp:+.2f}", (tp, i), xytext=(4, 0), textcoords="offset points", va="center", fontsize=8)
    ax1.set_yticks(y, [r["label"] for r in rows])
    ax1.margins(x=0.15)
    ax1.set_xlabel("LUFS, louder to the right (filled: integrated, open: max short-term)")
    ax2.axvline(0, color="#333", lw=0.8)
    ax2.margins(x=0.25)
    ax2.set_xlabel("True peak (dBTP)")
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    fig.text(0.01, -0.02, "All tracks at 0 dBFS sample peak: equal sample peaks do not mean equal loudness. "
             "True peak is measured separately.", fontsize=8, color="#555")
    fig.tight_layout()
    return _png(fig)


# --- Tonal balance ---


def tonal_curves(curves: list[dict], title: str, reference_index: int = 0, note: str = "") -> bytes:
    """curves: [{track_id, label, centres, level_db}]; the lower panel shows differences from curves[reference_index]."""
    fig, (ax, axd) = plt.subplots(2, 1, figsize=(WIDTH_IN, 6.0), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    base = curves[reference_index]
    for c in curves:
        style = "--" if c["track_id"] == "combined" else "-"
        ax.plot(c["centres"], c["level_db"], style, color=colour(c["track_id"]), lw=1.8, label=c["label"])
        if c is not base:
            axd.plot(c["centres"], base["level_db"] - c["level_db"], style, color=colour(c["track_id"]), lw=1.6,
                     label=c["label"])
    axd.axhline(0, color="#333", lw=0.8)
    ax.set_ylabel("Level (dB, 1/6-octave bands)")
    axd.set_ylabel("Difference (dB)")
    # Spectra leave the lower middle of the top panel and the upper right of the difference panel empty.
    ax.legend(loc="lower center", fontsize=8, frameon=False)
    axd.legend(loc="upper right", fontsize=8, frameon=False, title=f"{base['label']} minus", title_fontsize=8)
    _hz(axd)
    ax.set_xlim(20, 20000)
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    fig.text(0.01, -0.01, (note or "Power-averaged spectrum, each track normalised once to 0 dBFS sample peak.")
             + " At 1/6 octave, peaks and dips of a few dB between about 100 Hz and 1 kHz follow each track's notes "
             "and key, not EQ.", fontsize=8, color="#555", wrap=True)
    fig.tight_layout()
    return _png(fig)


# --- Stereo ---


def stereo_by_frequency(results: list[dict], title: str) -> bytes:
    """results: [{track_id, label, centres, side_to_mid_db, mono_change_db}]."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(WIDTH_IN, 6.0), sharex=True)
    for r in results:
        ax1.plot(r["centres"], r["side_to_mid_db"], color=colour(r["track_id"]), lw=1.8, marker="o", ms=3, label=r["label"])
        ax2.plot(r["centres"], r["mono_change_db"], color=colour(r["track_id"]), lw=1.8, marker="o", ms=3, label=r["label"])
    ax1.set_ylabel("Side relative to mid (dB)")
    lowest = min((np.nanmin(r["side_to_mid_db"]) for r in results if np.isfinite(r["side_to_mid_db"]).any()), default=-40.0)
    ax1.set_ylim(max(-62.0, min(-40.0, np.floor(lowest / 10) * 10)), 5)  # ratios are clamped at -60 dB
    ax2.axhline(-3.01, color="#888", lw=0.8, ls=":")
    ax2.text(22, -3.01, " unrelated L/R (fold-down scaling)", fontsize=8, color="#666", va="bottom")
    ax2.axhline(0, color="#333", lw=0.8)
    ax2.set_ylabel("Level change in mono (dB)")
    ax2.set_ylim(-12, 1)
    ax1.legend(loc="lower right", fontsize=8)
    _hz(ax2)
    ax1.set_xlim(20, 20000)
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    fig.text(0.01, -0.01, "1/3-octave bands. Mid = (L+R)/2, side = (L-R)/2; mono change = mid power vs average "
             "channel power. Below about -6 dB suggests cancellation.", fontsize=8, color="#555")
    fig.tight_layout()
    return _png(fig)


def correlation_timeline(results: list[dict], title: str) -> bytes:
    """results: [{track_id, label, times_s, correlation}]."""
    rows = len(results)
    fig, axes = plt.subplots(rows, 1, figsize=(WIDTH_IN, 1.6 * rows + 0.6), sharey=True, squeeze=False)
    for ax, r in zip(axes[:, 0], results):
        ax.plot(r["times_s"], r["correlation"], color=colour(r["track_id"]), lw=0.9)
        ax.axhline(0, color="#333", lw=0.8)
        ax.set_ylim(-1, 1)
        ax.set_ylabel("L/R correlation")
        ax.set_title(r["label"], fontsize=10, loc="left")
        _mmss(ax)
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    fig.text(0.01, -0.01, "Correlation coefficient over 400 ms windows: +1 mono, 0 unrelated, negative = opposite polarity.",
             fontsize=8, color="#555")
    fig.tight_layout()
    return _png(fig)


# --- Kick ---


def kick_envelopes(kicks: list[dict], title: str) -> bytes:
    """kicks: [{track_id, label, envelope_ms, envelope_db}] in parent-track normalisation."""
    fig, ax = plt.subplots(figsize=(WIDTH_IN, 3.8))
    for k in kicks:
        ax.plot(k["envelope_ms"], k["envelope_db"], color=colour(k["track_id"]), lw=1.8, label=k["label"])
    ax.set_xlim(-10, 400)
    top = max(np.max(k["envelope_db"]) for k in kicks)
    ax.set_ylim(top - 45, top + 3)
    ax.set_xlabel("Time from kick onset (ms)")
    ax.set_ylabel("30-120 Hz level (dB)")
    ax.legend(fontsize=8)
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    fig.text(0.01, -0.02, "Body-band (30-120 Hz) analytic envelope, 5 ms smoothing, of each selected kick, at its track's whole-track peak normalisation.",
             fontsize=8, color="#555")
    fig.tight_layout()
    return _png(fig)


def kick_spectrograms(kicks: list[dict], title: str) -> bytes:
    """kicks: [{track_id, label, crop, sample_rate}]: one panel per kick on one colour scale."""
    n_fft, hop, win = 4096, 64, 1024
    panels = []
    for k in kicks:
        x = k["crop"]
        frames = np.lib.stride_tricks.sliding_window_view(np.pad(x, (win // 2, win // 2)), win)[::hop]
        spec = np.abs(np.fft.rfft(frames * np.hanning(win), n_fft, axis=1)) ** 2 / np.sum(np.hanning(win) ** 2)
        panels.append((k, bands.db(spec.T), np.arange(len(frames)) * hop / k["sample_rate"] * 1000 - 10,
                       np.fft.rfftfreq(n_fft, 1 / k["sample_rate"])))
    vmax = max(np.max(p[1]) for p in panels)
    fig, axes = plt.subplots(1, len(panels), figsize=(WIDTH_IN, 3.6), sharey=True, squeeze=False)
    mesh = None
    for ax, (k, s, t, f) in zip(axes[0], panels):
        keep = (f >= 20) & (f <= 16000)
        mesh = ax.pcolormesh(t, f[keep], s[keep], shading="auto", cmap="magma", vmin=vmax - 80, vmax=vmax)
        ax.set_yscale("log")
        ax.set_title(k["label"], fontsize=9, loc="left")
        ax.set_xlabel("ms from onset")
        ax.grid(False)
    axes[0][0].set_ylabel("Frequency (Hz)")
    axes[0][0].yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"))
    fig.colorbar(mesh, ax=axes[0].tolist(), label="dB (shared scale)", fraction=0.02)
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    return _png(fig)


# --- Low end ---


def recovery_envelopes(results: list[dict], band_names: list[str], title: str) -> bytes:
    """results: [{track_id, label, bpm, recoveries: {band: BandRecovery}}]; one panel per band."""
    fig, axes = plt.subplots(1, len(band_names), figsize=(WIDTH_IN, 3.6), sharey=True, squeeze=False)
    for ax, name in zip(axes[0], band_names):
        for r in results:
            rec = r["recoveries"][name]
            if not len(rec.curve_time_ms):
                continue
            c = colour(r["track_id"])
            ax.plot(rec.curve_time_ms, rec.curve_median_db, color=c, lw=1.6,
                    label=r["label"] + ("" if rec.available else " (estimate unavailable)"))
            ax.fill_between(rec.curve_time_ms, rec.curve_q1_db, rec.curve_q3_db, color=c, alpha=0.15, lw=0)
            if rec.available:
                for t in (rec.dip_ms, rec.recovered_at_ms):
                    ax.plot(t, np.interp(t, rec.curve_time_ms, rec.curve_median_db), "o", color=c, ms=4)
        if results:
            beat_ms = 60000.0 / results[0]["bpm"]
            ax.axvspan(0.5 * beat_ms, 0.8 * beat_ms, color="#999", alpha=0.12, lw=0)
        lo, hi = rec.band_hz if results else (0, 0)
        ax.set_title(f"{name.replace('_', ' ')} ({lo:g}-{hi:g} Hz)", fontsize=9, loc="left")
        ax.axhline(0, color="#333", lw=0.8)
        ax.set_xlabel("ms after kick")
        ax.set_ylim(-30, 25)
    axes[0][0].set_ylabel("Level re baseline (dB)")
    axes[0][-1].legend(fontsize=7, loc="upper right")
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    fig.text(0.01, -0.03, "Median band envelope across kick events (shaded: middle 50%), relative to each event's "
             "baseline (median level from 50% to 80% of the beat, shaded grey). A band-passed mix contains every "
             "source in that band; the kick dominates the sub and low bands.", fontsize=8, color="#555")
    fig.tight_layout()
    return _png(fig)


def low_end_distribution(results: list[dict], title: str) -> bytes:
    """results: [{track_id, label, centres, level_db}] for 20-300 Hz."""
    fig, ax = plt.subplots(figsize=(WIDTH_IN, 3.6))
    for r in results:
        ax.plot(r["centres"], r["level_db"], color=colour(r["track_id"]), lw=1.8, marker="o", ms=3, label=r["label"])
    _hz(ax)
    ax.set_xlim(20, 300)
    ax.set_ylabel("Level (dB, 1/6-octave bands)")
    ax.legend(fontsize=8)
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    fig.text(0.01, -0.02, "Power-averaged over the passage; each track normalised once to 0 dBFS sample peak. "
             "Below about 40 Hz the analysis resolution is coarser than the bands.", fontsize=8, color="#555")
    fig.tight_layout()
    return _png(fig)


# --- Spectrogram ---

SPECTROGRAM_PRESETS = {
    "overview": {"n_fft": 4096, "hop": 1024, "fmax": 20000.0},
    "low_detail": {"n_fft": 8192, "hop": 256, "fmax": 400.0},
    "transient": {"n_fft": 1024, "hop": 128, "fmax": 20000.0},
}


def spectrograms(panels: list[dict], preset: str, title: str, fmin: float = 20.0, fmax: float | None = None,
                 markers: dict[str, list[float]] | None = None) -> bytes:
    """panels: [{track_id, label, signal (mono), sample_rate, start_s}], one row each, one shared dB scale."""
    p = SPECTROGRAM_PRESETS[preset]
    fmax = fmax or p["fmax"]
    computed = []
    for panel in panels:
        x, sr = panel["signal"], panel["sample_rate"]
        win = np.hanning(p["n_fft"])
        frames = np.lib.stride_tricks.sliding_window_view(np.pad(x, (p["n_fft"] // 2,) * 2), p["n_fft"])[:: p["hop"]]
        spec = np.abs(np.fft.rfft(frames * win, axis=1)) ** 2 * 2 / (p["n_fft"] * np.sum(win**2))
        f = np.fft.rfftfreq(p["n_fft"], 1 / sr)
        keep = (f >= fmin) & (f <= fmax)
        computed.append((panel, panel["start_s"] + np.arange(len(frames)) * p["hop"] / sr, f[keep], bands.db(spec[:, keep].T)))
    vmax = max(np.max(c[3]) for c in computed)
    fig, axes = plt.subplots(len(computed), 1, figsize=(WIDTH_IN, 2.4 * len(computed) + 0.6), squeeze=False, sharey=True)
    mesh = None
    for row, (ax, (panel, t, f, s)) in enumerate(zip(axes[:, 0], computed)):
        mesh = ax.pcolormesh(t, f, s, shading="auto", cmap="magma", vmin=vmax - 90, vmax=vmax)
        if fmax / max(fmin, 1) > 30:
            ax.set_yscale("log")
        for m in (markers or {}).get(panel["track_id"], []):
            ax.axvline(m, color="#00e5ff", lw=1.0, alpha=0.9)
        ax.set_title(panel["label"], fontsize=9, loc="left")
        ax.set_ylabel("Hz")
        ax.grid(False)
        _mmss(ax) if t[-1] - t[0] > 20 else ax.set_xlabel("Time (s)")
        if row < len(computed) - 1:
            ax.set_xlabel("")
    fig.subplots_adjust(hspace=0.45)
    fig.colorbar(mesh, ax=axes[:, 0].tolist(), label="dB (shared scale)", fraction=0.02)
    fig.suptitle(title, fontweight="bold", x=0.01, ha="left")
    return _png(fig)
