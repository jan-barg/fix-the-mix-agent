"""Chart layout: no text in a chart overlaps other text, with four tracks and long labels (the worst case)."""

import matplotlib.pyplot as plt
import numpy as np
import pytest
from matplotlib.transforms import Bbox

from app.audio import figures
from app.audio.lowend import BandRecovery

IDS = ["user", "ref1", "ref2", "ref3"]
LONG = ["Your track, drop 1 1:10–1:34", "Your track, drop 2 2:20–2:49", "Reference 1, drop 1 1:13–1:57",
        "Reference 1, drop 2 3:00–3:44"]


def _texts(fig):
    """Every drawn text: titles, labels, annotations, legends, and the tick labels inside each axis's view."""
    out = [t for t in fig.texts]
    for ax in fig.axes:
        out += [ax.title, ax._left_title, ax._right_title, ax.xaxis.label, ax.yaxis.label, *ax.texts]
        if ax.get_legend():
            out += ax.get_legend().get_texts()
        for axis in (ax.xaxis, ax.yaxis):
            lo, hi = sorted(axis.get_view_interval())
            span = hi - lo
            for tick in axis.get_major_ticks():
                if lo - 1e-6 * span <= tick.get_loc() <= hi + 1e-6 * span:
                    out.append(tick.label1)
    for leg in fig.legends:
        out += leg.get_texts()
    return [t for t in out if t.get_visible() and t.get_text().strip()]


def _overlaps(fig) -> list[tuple[str, str]]:
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    boxes = []
    for t in _texts(fig):
        bb = t.get_window_extent(renderer)
        clipped = t.get_clip_on() and (t.get_clip_path() is not None or t.get_clip_box() is not None)
        if clipped:  # only the part inside the axes is drawn (titles carry clip_on but no clip region)
            bb = Bbox.intersection(bb, t.axes.bbox)
            if bb is None:
                continue
        boxes.append((t.get_text(), bb))
    found = []
    for i, (a, ba) in enumerate(boxes):
        for b, bb in boxes[i + 1:]:
            w = min(ba.x1, bb.x1) - max(ba.x0, bb.x0)
            h = min(ba.y1, bb.y1) - max(ba.y0, bb.y0)
            if w > 1 and h > 1:  # more than touching
                found.append((a, b))
    return found


@pytest.fixture
def drawn(monkeypatch):
    """Keep each figure open after drawing so its text can be checked."""
    kept = []
    monkeypatch.setattr(figures, "_png", lambda fig: kept.append(fig) or b"")
    yield kept
    plt.close("all")


def _check(kept):
    assert kept
    for fig in kept:
        assert _overlaps(fig) == []


def _recovery(lo, hi, seed):
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 450, 200)
    median = 12 * np.exp(-t / 40) - 14 * np.exp(-((t - 120) / 60) ** 2) + rng.normal(0, 0.5, t.size)
    return BandRecovery("b", (lo, hi), True, "", 40, dip_ms=120.0, recovered_at_ms=220.0, curve_time_ms=t,
                        curve_median_db=median, curve_q1_db=median - 3, curve_q3_db=median + 3)


def test_recovery_envelopes(drawn):
    bands = {"sub": (30, 60), "low": (60, 120), "upper_bass": (120, 300), "low_mid": (300, 600)}
    results = [{"track_id": tid, "label": label, "bpm": 136.0,
                "recoveries": {n: _recovery(*hz, i) for n, hz in bands.items()}}
               for i, (tid, label) in enumerate(zip(["user", "user", "ref1", "ref1"], LONG))]
    figures.recovery_envelopes(results, list(bands))
    figures.recovery_envelopes(results[:2], list(bands))
    _check(drawn)


def test_loudness_charts(drawn):
    t = np.arange(0, 240, 0.1)
    names = ["Your track", "Reference 1", "Reference 2", "Reference 3"]
    series = [{"track_id": tid, "name": name, "times_s": t, "short_term_lufs": -12 + 4 * np.sin(t / 20 + i),
               "bar_s": 240 / (128 + 2 * i)} for i, (tid, name) in enumerate(zip(IDS, names))]
    # Short sections side by side: their labels must not run into each other.
    sections = {tid: [{"label": lab, "start_s": a, "end_s": b} for lab, a, b in
                      [("intro", 0, 30), ("breakdown", 30, 34), ("build", 34, 38), ("drop", 38, 90),
                       ("breakdown", 90, 200), ("outro", 200, 240)]] for tid in IDS}
    figures.loudness_timeline(series, sections, {tid: (38.0 + i, True) for i, tid in enumerate(IDS)}, 32)
    figures.loudness_timeline(series[:2], sections, {"user": (38.0, True), "ref1": (0.0, False)})
    rows = [{"track_id": tid, "label": label, "integrated_lufs": -8.0 - i, "max_short_term_lufs": -6.5 - i,
             "true_peak_dbtp": 0.4 - i} for i, (tid, label) in enumerate(zip(IDS, LONG))]
    figures.loudness_comparison(rows)
    _check(drawn)


def test_spectrum_and_stereo_charts(drawn):
    centres = np.geomspace(20, 20000, 60)
    curves = [{"track_id": tid, "label": label, "centres": centres, "level_db": -30 - 10 * np.log10(centres / 20) + i}
              for i, (tid, label) in enumerate(zip(IDS, LONG))]
    figures.tonal_curves(curves)
    low = centres[centres <= 300]
    figures.low_end_distribution([{"track_id": tid, "label": label, "centres": low, "level_db": -20 - i + 0 * low}
                                  for i, (tid, label) in enumerate(zip(IDS, LONG))])
    third = np.geomspace(25, 16000, 28)
    figures.stereo_by_frequency([{"track_id": tid, "label": label, "centres": third,
                                  "side_to_mid_db": -20 + 0 * third - i, "mono_change_db": -1 + 0 * third}
                                 for i, (tid, label) in enumerate(zip(IDS, LONG))])
    times = np.arange(0, 120, 0.4)
    figures.correlation_timeline([{"track_id": tid, "label": label, "times_s": times,
                                   "correlation": np.cos(times / 10)} for tid, label in zip(IDS, LONG)])
    _check(drawn)


def test_kick_and_spectrogram_charts(drawn):
    sr = 44100
    ms = np.arange(-10, 400, 1.0)
    figures.kick_envelopes([{"track_id": tid, "label": label, "envelope_ms": ms, "envelope_db": -10 - ms / 20 - i}
                            for i, (tid, label) in enumerate(zip(IDS, LONG))])
    rng = np.random.default_rng(0)
    crop = rng.normal(0, 0.1, int(0.41 * sr))
    figures.kick_spectrograms([{"track_id": tid, "label": f"{name}, kick at 3:56", "crop": crop, "sample_rate": sr}
                               for tid, name in zip(IDS, ["Your track", "Reference 1", "Reference 2", "Reference 3"])])
    signal = rng.normal(0, 0.1, 30 * sr)
    figures.spectrograms([{"track_id": tid, "label": label, "signal": signal, "sample_rate": sr, "start_s": 60.0}
                          for tid, label in zip(IDS[:2], LONG[:2])], "overview", markers={"user": [61.0, 62.0]})
    _check(drawn)
