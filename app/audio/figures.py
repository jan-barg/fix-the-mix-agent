"""Every chart, drawn only from measured arrays (SPEC §8.2).

One dark, studio-style look shared with the app (static/styles.css) and the report template: the same
background, ink and track colours, so a chart reads as part of the page it sits on. Track colours are
fixed for the whole report: your track, References 1-3 and a combined target. Comparisons share axes
and colour scales so equal levels look equal. Titles live in the report's captions, not in the image.
"""

import io
import logging

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402
from PIL import Image  # noqa: E402

from app.audio import bands  # noqa: E402

# The app's palette (static/styles.css): background, ink and the shared track colours.
BG, INK, INK_2, MUTED = "#111419", "#eceef1", "#b6bdc6", "#7d8692"
GRID, AXIS, ZERO = "#1f242c", "#353c47", "#59616d"
TRACK_COLOURS = {"user": "#ef5b6b", "ref1": "#3fa7d6", "ref2": "#4cc27a", "ref3": "#a98be0", "combined": "#e6e8eb"}
# Section colours of the section editor (static/editor.js).
SECTION_COLOURS = {"intro": "#5b8def", "build": "#f2b134", "drop": "#ef4b5f", "breakdown": "#3fbf8f", "verse": "#8f7cf0",
                   "chorus": "#f07cc0", "bridge": "#5cc9e6", "outro": "#7a86a8", "unknown": "#5d6672"}
CUSTOM_SECTION = "#b59a6a"
SPECTRO_CMAP = "magma"
# 7.5 in at 200 dpi: 1500 px wide. Text sizes are in points, so the narrower figure keeps labels readable
# where the report shows a chart (about 700 px on screen, 6.7 in on an A4 page).
WIDTH_IN, DPI = 7.5, 200

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)  # Inter is missing on some dev machines
plt.rcParams.update({
    "font.family": "sans-serif", "font.sans-serif": ["Inter", "Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 9.5, "text.color": INK_2,
    "figure.facecolor": BG, "savefig.facecolor": BG, "axes.facecolor": BG,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.labelcolor": MUTED, "axes.labelsize": 8.5, "axes.labelpad": 7,
    "axes.titlesize": 9.5, "axes.titleweight": "semibold", "axes.titlecolor": INK_2, "axes.titlelocation": "left",
    "axes.titlepad": 9, "axes.spines.top": False, "axes.spines.right": False, "axes.spines.left": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelsize": 8, "ytick.labelsize": 8,
    "xtick.major.size": 0, "ytick.major.size": 0, "xtick.minor.size": 0, "ytick.minor.size": 0,
    "xtick.major.pad": 6, "ytick.major.pad": 6,
    "legend.frameon": False, "legend.fontsize": 8.5, "legend.labelcolor": INK_2, "legend.handlelength": 1.4,
    "legend.handleheight": 0.6, "legend.columnspacing": 1.6,
    "lines.linewidth": 1.8, "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
})


def colour(track_id: str) -> str:
    return TRACK_COLOURS.get(track_id, "#999999")


def section_colour(label: str) -> str:
    return SECTION_COLOURS.get(label.split()[0].lower(), CUSTOM_SECTION)


def _size(height: float) -> tuple[float, float]:
    """A figure size from its height as designed at 10 in wide, keeping the aspect ratio."""
    return WIDTH_IN, height * WIDTH_IN / 10


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", pad_inches=0.22)
    # A stray artist far outside the axes stretches the tight crop; models reject images over 8000 px. Fall back
    # to the figure's own size, which crops the stray artist away.
    width, height = fig.get_size_inches() * DPI
    if max(Image.open(io.BytesIO(buf.getvalue())).size) > 2 * max(width, height):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=DPI)
    plt.close(fig)
    return buf.getvalue()


def _columns(labels: list[str]) -> int:
    """As many legend columns (up to 4) as fit across the figure: a wider legend widens the tight-cropped image
    and shrinks the chart on the page."""
    fontsize = plt.rcParams["legend.fontsize"]
    entry = 30 + 0.55 * fontsize * max(map(len, labels))  # points: handle and gap, then the longest label
    fit = int((WIDTH_IN * 72 - 30) // (entry + 1.6 * fontsize))
    return max(1, min(len(labels), 4, fit))


def _legend(target, entries: list[tuple[str, str, str]], *, above=None) -> None:
    """A row of legend entries above the plot: (label, colour, line style)."""
    handles = [Line2D([], [], color=c, lw=2.6, ls=ls, solid_capstyle="round") for _, c, ls in entries]
    labels = [label for label, _, _ in entries]
    if above is None:  # an Axes
        target.legend(handles, labels, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=_columns(labels),
                      borderaxespad=0.4)
    else:  # a Figure: at its top, aligned with the given axes
        target.legend(handles, labels, loc="upper left", bbox_to_anchor=(above.get_position().x0, 1.0),
                      ncol=_columns(labels), borderaxespad=0.2)


def _note(fig, text: str) -> None:
    """A method note under everything drawn so far (tick labels and axis labels included)."""
    renderer = fig.canvas.get_renderer()
    to_fig = fig.transFigure.inverted()
    bottom = min(ax.get_tightbbox(renderer).transformed(to_fig).y0 for ax in fig.axes)
    fig.text(0.0, bottom - 0.03, text, fontsize=7.6, color=MUTED, ha="left", va="top", wrap=True)


def _zero(ax, y: float = 0.0) -> None:
    ax.axhline(y, color=ZERO, lw=0.9, zorder=1)


def _hz(ax):
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"))
    ax.xaxis.set_minor_formatter(FuncFormatter(lambda v, _: ""))
    ax.grid(True, which="major", axis="both")
    ax.set_xlabel("Frequency (Hz)")


def _mmss(ax):
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v // 60)}:{int(v % 60):02d}"))
    ax.set_xlabel("Time (m:ss)")


def _colorbar(fig, mesh, axes, label: str) -> None:
    cb = fig.colorbar(mesh, ax=axes, fraction=0.018, pad=0.015)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=MUTED, labelsize=7.5, length=0)
    cb.set_label(label, color=MUTED, fontsize=8)


# --- Loudness ---


def loudness_timeline(series: list[dict], sections: dict[str, list[dict]] | None = None) -> bytes:
    """series: [{track_id, label, times_s, short_term_lufs}]; sections: track_id -> [{label, start_s, end_s}]."""
    rows = len(series)
    # Row heights in inches, not scaled: each row's title and the tick labels above it need fixed room.
    fig, axes = plt.subplots(rows, 1, figsize=(WIDTH_IN, 1.6 * rows + 0.4), sharey=True, squeeze=False)
    finite = np.concatenate([s["short_term_lufs"][np.isfinite(s["short_term_lufs"])] for s in series])
    lo, hi = (np.floor(finite.min() / 3) * 3 - 1, np.ceil(finite.max() / 3) * 3 + 2) if finite.size else (-40, 0)
    for ax, s in zip(axes[:, 0], series):
        c = colour(s["track_id"])
        x0, x1 = (s["times_s"][0], s["times_s"][-1]) if len(s["times_s"]) else (0, 1)
        for sec in (sections or {}).get(s["track_id"], []):  # a strip along the bottom, like the section editor
            # Only the part inside the plotted passage: a label outside it would stretch the tight-cropped image.
            start, end = max(sec["start_s"], x0), min(sec["end_s"], x1)
            if end <= start:
                continue
            sc = section_colour(sec["label"])
            ax.axvspan(start, end, color=sc, alpha=0.06, lw=0, zorder=0)
            ax.plot([start, end], [lo, lo], color=sc, lw=4, solid_capstyle="butt", zorder=3, clip_on=False)
            ax.text(start + (x1 - x0) * 0.004, lo + 1.0, sec["label"], ha="left", va="bottom", fontsize=7.5,
                    color=sc, zorder=3, clip_on=True)
        y = s["short_term_lufs"]
        ax.fill_between(s["times_s"], y, lo, color=c, alpha=0.05, lw=0, zorder=1)
        ax.plot(s["times_s"], y, color=c, lw=1.3, zorder=2)
        ax.set_ylim(lo, hi)
        ax.set_xlim(x0, x1)
        ax.set_ylabel("LUFS")
        ax.set_title(s["label"], color=c)
        _mmss(ax)
        if ax is not axes[-1, 0]:
            ax.set_xlabel("")
    fig.subplots_adjust(hspace=0.8)
    _note(fig, "Short-term loudness (3 s window). Each track normalised once to 0 dBFS sample peak.")
    return _png(fig)


def loudness_comparison(rows: list[dict]) -> bytes:
    """rows: [{track_id, label, integrated_lufs, max_short_term_lufs, true_peak_dbtp}]; any value may be
    None (a passage too short or silent to meter), and is then left out rather than drawn."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=_size(0.4 * len(rows) + 1.5), sharey=True,
                                   gridspec_kw={"width_ratios": [2.3, 1], "wspace": 0.08})
    y = np.arange(len(rows))[::-1]  # first row at the top
    for i, r in zip(y, rows):
        c = colour(r["track_id"])
        st, integrated, tp = r["max_short_term_lufs"], r["integrated_lufs"], r["true_peak_dbtp"]
        if integrated is not None and st is not None:
            ax1.plot([integrated, st], [i, i], color=c, lw=2.2, alpha=0.35, solid_capstyle="round")
        if integrated is not None:
            ax1.plot(integrated, i, "o", color=c, ms=7.5, zorder=3)
            ax1.annotate(f"{integrated:.1f}", (integrated, i), xytext=(-8, 0), textcoords="offset points",
                         ha="right", va="center", fontsize=7.8, color=INK_2)
        if st is not None:
            ax1.plot(st, i, "o", mfc=BG, mec=c, mew=1.8, ms=7.5, zorder=3)
            ax1.annotate(f"{st:.1f}", (st, i), xytext=(8, 0), textcoords="offset points", va="center",
                         fontsize=7.8, color=INK_2)
        if tp is not None and np.isfinite(tp):
            ax2.barh(i, tp, height=0.42, color=c, alpha=0.85, lw=0)
            ax2.annotate(f"{tp:+.2f}", (tp, i), xytext=(5 if tp >= 0 else -5, 0), textcoords="offset points",
                         ha="left" if tp >= 0 else "right", va="center", fontsize=7.8, color=INK_2)
    ax1.set_yticks(y, [r["label"] for r in rows])
    ax1.tick_params(axis="y", labelcolor=INK_2, labelsize=8.2)
    ax1.grid(True, axis="x")
    ax1.grid(False, axis="y")
    ax1.margins(x=0.14, y=0.12)
    ax1.set_xlabel("LUFS, louder to the right")
    ax1.legend([Line2D([], [], marker="o", ls="", color=INK_2, ms=6),
                Line2D([], [], marker="o", ls="", mfc=BG, mec=INK_2, mew=1.5, ms=6)],
               ["Integrated", "Max short-term"], loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, borderaxespad=0.4)
    ax2.axvline(0, color=ZERO, lw=0.9)
    ax2.grid(True, axis="x")
    ax2.grid(False, axis="y")
    ax2.margins(x=0.3)
    ax2.set_xlabel("True peak (dBTP)")
    _note(fig, "All tracks at 0 dBFS sample peak: equal sample peaks do not mean equal loudness. True peak is "
               "measured separately.")
    return _png(fig)


# --- Tonal balance ---


def tonal_curves(curves: list[dict], reference_index: int = 0, note: str = "") -> bytes:
    """curves: [{track_id, label, centres, level_db}]; the lower panel shows differences from curves[reference_index]."""
    fig, (ax, axd) = plt.subplots(2, 1, figsize=_size(5.8), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 2], "hspace": 0.12})
    base = curves[reference_index]
    for c in curves:
        style = "--" if c["track_id"] == "combined" else "-"
        col = colour(c["track_id"])
        if c is base:
            ax.fill_between(c["centres"], c["level_db"], np.nanmin(c["level_db"]) - 30, color=col, alpha=0.06, lw=0)
        ax.plot(c["centres"], c["level_db"], style, color=col, lw=1.9)
        if c is not base:
            axd.plot(c["centres"], base["level_db"] - c["level_db"], style, color=col, lw=1.7)
    finite = np.concatenate([np.asarray(c["level_db"])[np.isfinite(c["level_db"])] for c in curves])
    if finite.size:
        ax.set_ylim(np.floor(finite.min() / 10) * 10 - 2, np.ceil(finite.max() / 10) * 10 + 2)
    _zero(axd)
    ax.set_ylabel("Level (dB, 1/6 octave)")
    axd.set_ylabel("Difference (dB)")
    _legend(ax, [(c["label"], colour(c["track_id"]), "--" if c["track_id"] == "combined" else "-") for c in curves])
    _hz(axd)
    ax.grid(True, which="major", axis="both")
    ax.set_xlim(20, 20000)
    _note(fig, (note or "Power-averaged spectrum, each track normalised once to 0 dBFS sample peak.")
          + f" Lower panel: {base['label']} minus each other track. At 1/6 octave, peaks and dips of a few dB between "
          "about 100 Hz and 1 kHz follow each track's notes and key, not EQ.")
    return _png(fig)


# --- Stereo ---


def stereo_by_frequency(results: list[dict]) -> bytes:
    """results: [{track_id, label, centres, side_to_mid_db, mono_change_db}]."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=_size(5.6), sharex=True, gridspec_kw={"hspace": 0.14})
    for r in results:
        c = colour(r["track_id"])
        ax1.plot(r["centres"], r["side_to_mid_db"], color=c, lw=1.8, marker="o", ms=3.2, mec=c)
        ax2.plot(r["centres"], r["mono_change_db"], color=c, lw=1.8, marker="o", ms=3.2, mec=c)
    ax1.set_ylabel("Side re mid (dB)")
    lowest = min((np.nanmin(r["side_to_mid_db"]) for r in results if np.isfinite(r["side_to_mid_db"]).any()), default=-40.0)
    ax1.set_ylim(max(-62.0, min(-40.0, np.floor(lowest / 10) * 10)), 5)  # ratios are clamped at -60 dB
    ax1.text(21, 3.5, "higher = wider", fontsize=7.5, color=MUTED, va="top")
    ax2.axhline(-3.01, color=MUTED, lw=0.9, ls=(0, (2, 3)))
    ax2.text(21, -3.01, "unrelated L/R (fold-down scaling)", fontsize=7.5, color=MUTED, va="bottom")
    _zero(ax2)
    ax2.set_ylabel("Change in mono (dB)")
    ax2.set_ylim(-12, 1)
    _legend(ax1, [(r["label"], colour(r["track_id"]), "-") for r in results])
    _hz(ax2)
    ax1.grid(True, which="major", axis="both")
    ax1.set_xlim(20, 20000)
    _note(fig, "1/3-octave bands. Mid = (L+R)/2, side = (L−R)/2; mono change = mid power vs average channel power. "
               "Below about −6 dB suggests cancellation.")
    return _png(fig)


def correlation_timeline(results: list[dict]) -> bytes:
    """results: [{track_id, label, times_s, correlation}]."""
    rows = len(results)
    fig, axes = plt.subplots(rows, 1, figsize=(WIDTH_IN, 1.45 * rows + 0.4), sharey=True, squeeze=False)
    for ax, r in zip(axes[:, 0], results):
        c = colour(r["track_id"])
        corr = np.asarray(r["correlation"])
        ax.fill_between(r["times_s"], corr, 0, where=corr >= 0, color=c, alpha=0.14, lw=0, interpolate=True)
        ax.fill_between(r["times_s"], corr, 0, where=corr < 0, color="#ff6b6b", alpha=0.35, lw=0, interpolate=True)
        ax.plot(r["times_s"], corr, color=c, lw=1.0)
        _zero(ax)
        ax.set_ylim(-1, 1.05)
        ax.set_yticks([-1, 0, 1])
        ax.set_ylabel("Correlation")
        ax.set_title(r["label"], color=c)
        if len(r["times_s"]):
            ax.set_xlim(r["times_s"][0], r["times_s"][-1])
        _mmss(ax)
        if ax is not axes[-1, 0]:
            ax.set_xlabel("")
    fig.subplots_adjust(hspace=0.9)
    _note(fig, "L/R correlation over 400 ms windows: +1 mono, 0 unrelated, negative (shaded red) = opposite polarity.")
    return _png(fig)


# --- Kick ---


def kick_envelopes(kicks: list[dict]) -> bytes:
    """kicks: [{track_id, label, envelope_ms, envelope_db}] in parent-track normalisation."""
    fig, ax = plt.subplots(figsize=_size(3.6))
    top = max(np.max(k["envelope_db"]) for k in kicks)
    for k in kicks:
        c = colour(k["track_id"])
        ax.fill_between(k["envelope_ms"], k["envelope_db"], top - 45, color=c, alpha=0.08, lw=0)
        ax.plot(k["envelope_ms"], k["envelope_db"], color=c, lw=1.9)
    ax.set_xlim(-10, 400)
    ax.set_ylim(top - 45, top + 3)
    ax.axvline(0, color=ZERO, lw=0.9)
    ax.set_xlabel("Time from kick onset (ms)")
    ax.set_ylabel("30–120 Hz level (dB)")
    _legend(ax, [(k["label"], colour(k["track_id"]), "-") for k in kicks])
    _note(fig, "Body-band (30–120 Hz) analytic envelope, 5 ms smoothing, of each selected kick, at its track's "
               "whole-track peak normalisation.")
    return _png(fig)


def kick_spectrograms(kicks: list[dict]) -> bytes:
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
    fig, axes = plt.subplots(1, len(panels), figsize=_size(3.3), sharey=True, squeeze=False,
                             gridspec_kw={"wspace": 0.06})
    mesh = None
    for ax, (k, s, t, f) in zip(axes[0], panels):
        keep = (f >= 20) & (f <= 16000)
        mesh = ax.pcolormesh(t, f[keep], s[keep], shading="auto", cmap=SPECTRO_CMAP, vmin=vmax - 80, vmax=vmax,
                             rasterized=True)
        ax.set_yscale("log")
        # Two lines ("Reference 1" over "kick at 3:56") once the panels are too narrow for one.
        title = k["label"].replace(", ", "\n", 1) if len(panels) > 2 else k["label"]
        ax.set_title(title, color=colour(k["track_id"]), fontsize=8.5)
        ax.set_xlabel("ms from onset")
        ax.grid(False)
        ax.spines["bottom"].set_visible(False)
    axes[0][0].set_ylabel("Frequency (Hz)")
    axes[0][0].yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"))
    _colorbar(fig, mesh, axes[0].tolist(), "dB (shared scale)")
    return _png(fig)


# --- Low end ---


def recovery_envelopes(results: list[dict], band_names: list[str]) -> bytes:
    """results: [{track_id, label, bpm, recoveries: {band: BandRecovery}}]; one panel per band."""
    fig, axes = plt.subplots(1, len(band_names), figsize=_size(3.5), sharey=True, squeeze=False,
                             gridspec_kw={"wspace": 0.08})
    for ax, name in zip(axes[0], band_names):
        rec = None
        if results:
            beat_ms = 60000.0 / results[0]["bpm"]
            ax.axvspan(0.5 * beat_ms, 0.8 * beat_ms, color=INK, alpha=0.05, lw=0)
        for r in results:
            rec = r["recoveries"][name]
            if not len(rec.curve_time_ms):
                continue
            c = colour(r["track_id"])
            ax.fill_between(rec.curve_time_ms, rec.curve_q1_db, rec.curve_q3_db, color=c, alpha=0.14, lw=0)
            ax.plot(rec.curve_time_ms, rec.curve_median_db, color=c, lw=1.7, ls="-" if rec.available else (0, (3, 2)))
            if rec.available:
                for t in (rec.dip_ms, rec.recovered_at_ms):
                    ax.plot(t, np.interp(t, rec.curve_time_ms, rec.curve_median_db), "o", color=c, ms=4.5,
                            mec=BG, mew=1.0, zorder=4)
        lo, hi = rec.band_hz if rec is not None else (0, 0)
        ax.set_title(f"{name.replace('_', ' ').capitalize()}  {lo:g}–{hi:g} Hz", fontsize=8.8)
        _zero(ax)
        ax.set_xlabel("ms after kick")
        ax.set_ylim(-30, 25)
    entries = [(r["label"], colour(r["track_id"]), "-") for r in results]
    axes[0][0].set_ylabel("Level re baseline (dB)")
    if entries:
        fig.subplots_adjust(top=0.78)
        _legend(fig, entries, above=axes[0][0])
    _note(fig, "Median band envelope across kick events (band: middle 50%), relative to each event's baseline "
               "(median level from 50% to 80% of the beat, lightly shaded). Dots: deepest dip and recovery. Dashed: "
               "no reliable recovery estimate. A band-passed mix contains every source in that band; the kick "
               "dominates the sub and low bands.")
    return _png(fig)


def low_end_distribution(results: list[dict]) -> bytes:
    """results: [{track_id, label, centres, level_db}] for 20-300 Hz."""
    fig, ax = plt.subplots(figsize=_size(3.5))
    for r in results:
        c = colour(r["track_id"])
        ax.plot(r["centres"], r["level_db"], color=c, lw=1.9, marker="o", ms=3.4, mec=c)
    _hz(ax)
    ax.set_xticks([20, 30, 40, 60, 80, 100, 150, 200, 300])
    ax.set_xlim(20, 300)
    ax.set_ylabel("Level (dB, 1/6 octave)")
    _legend(ax, [(r["label"], colour(r["track_id"]), "-") for r in results])
    _note(fig, "Power-averaged over the passage; each track normalised once to 0 dBFS sample peak. Below about "
               "40 Hz the analysis resolution is coarser than the bands.")
    return _png(fig)


# --- Spectrogram ---

SPECTROGRAM_PRESETS = {
    "overview": {"n_fft": 4096, "hop": 1024, "fmax": 20000.0},
    "low_detail": {"n_fft": 8192, "hop": 256, "fmax": 400.0},
    "transient": {"n_fft": 1024, "hop": 128, "fmax": 20000.0},
}


def spectrograms(panels: list[dict], preset: str, fmin: float = 20.0, fmax: float | None = None,
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
    fig, axes = plt.subplots(len(computed), 1, figsize=_size(2.2 * len(computed) + 0.5), squeeze=False, sharey=True)
    mesh = None
    for row, (ax, (panel, t, f, s)) in enumerate(zip(axes[:, 0], computed)):
        mesh = ax.pcolormesh(t, f, s, shading="auto", cmap=SPECTRO_CMAP, vmin=vmax - 90, vmax=vmax, rasterized=True)
        if fmax / max(fmin, 1) > 30:
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"))
        for m in (markers or {}).get(panel["track_id"], []):
            ax.axvline(m, color="#9fe8ff", lw=0.9, alpha=0.75)
        ax.set_title(panel["label"], color=colour(panel["track_id"]), fontsize=8.8)
        ax.set_ylabel("Hz")
        ax.grid(False)
        ax.spines["bottom"].set_visible(False)
        _mmss(ax) if t[-1] - t[0] > 20 else ax.set_xlabel("Time (s)")
        if row < len(computed) - 1:
            ax.set_xlabel("")
    fig.subplots_adjust(hspace=0.5)
    _colorbar(fig, mesh, axes[:, 0].tolist(), "dB (shared scale)")
    if markers:
        _note(fig, "Thin lines mark the kick onsets.")
    return _png(fig)
