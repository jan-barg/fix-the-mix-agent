"""Run every measurement tool on the demo corpus and write evidence summaries and charts.

Run: uv run python scripts/demo_measurements.py [output dir, default .data/demo-evidence]

Until the section editor exists, each track's "drop" is approximated by its loudest 32 bars.
"""

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import tools  # noqa: E402
from app.audio import decode, features  # noqa: E402
from app.audio.features import Scope, TrackData  # noqa: E402
from app.config import settings  # noqa: E402
from app.storage import get_storage  # noqa: E402


def load_corpus() -> list[TrackData]:
    corpus = json.loads((ROOT / "demo" / "corpus.json").read_text())
    tracks = []
    for t in corpus["tracks"]:
        path = ROOT / "demo" / "audio" / t["file"]
        audio = decode.prepare(path, settings.analysis_sample_rate)
        tid = "user" if t["role"] == "user" else f"ref{t['slot']}"
        name = "Your track" if t["role"] == "user" else f"Reference {t['slot']}"
        tracks.append(TrackData(tid, name, t["role"], audio, features.get_or_compute(audio, features.file_sha256(path), get_storage())))
    return tracks


def loudest_bars(track: TrackData, n_bars: int = 32) -> Scope:
    L = track.features.loudness
    bar = 4 * track.features.grid.beat_s
    st = np.nan_to_num(L.short_term_lufs, nan=-99.0)
    width = int(n_bars * bar / 0.1)
    i = int(np.argmax(np.convolve(st, np.ones(width), "valid")))
    # Snap the start to the nearest downbeat.
    downbeats = track.features.grid.downbeats_s
    start = float(downbeats[np.argmin(np.abs(downbeats - L.times_s[i]))])
    return Scope(track, start, min(start + n_bars * bar, track.audio.duration_s), f"loudest {n_bars} bars")


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    tracks = load_corpus()
    print(f"loaded {len(tracks)} tracks in {time.perf_counter() - t0:.1f}s")
    user, refs = tracks[0], tracks[1:]
    whole = [features.whole_track(t) for t in tracks]
    drops = [loudest_bars(t) for t in tracks]

    runs = {
        "loudness": lambda: tools.measure_loudness(whole + drops),
        "tonal": lambda: tools.measure_tonal_balance(whole, combine=[1, 2, 3]),
        "tonal_drops_level_matched": lambda: tools.measure_tonal_balance(drops, mode="level_matched"),
        "stereo": lambda: tools.measure_stereo(drops),
        "kick_candidates_user": lambda: tools.find_kick_candidates(user),
        "low_end": lambda: tools.measure_low_end(drops[:2]),
        "spectrogram": lambda: tools.render_spectrogram(whole[:2]),
    }
    best_kicks = []
    for name, run in runs.items():
        t = time.perf_counter()
        ev = run()
        for i, fig in enumerate(ev.figures):
            (out / f"{name}_{i}.png").write_bytes(fig.png)
        (out / f"{name}.json").write_text(json.dumps(ev.to_dict(), indent=2, default=str))
        print(f"\n=== {name} ({time.perf_counter() - t:.1f}s, {len(ev.figures)} figures)\n{ev.summary_text()}")
        if name == "kick_candidates_user":
            best_kicks.append(ev)

    # Compare the best kick from each track.
    from app.audio import kick
    selections = []
    for t in tracks:
        c = kick.find_candidates(t, n=1)
        if c:
            selections.append((t, c[0].time_s, c[0].time_s + c[0].gap_s))
    ev = tools.analyse_kicks(selections)
    for i, fig in enumerate(ev.figures):
        (out / f"kicks_{i}.png").write_bytes(fig.png)
    print(f"\n=== kicks\n{ev.summary_text()}")
    print(f"\nwrote charts to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".data" / "demo-evidence")
