"""Prepare and meter audio files the way an upload is processed, and print a summary.

Run: uv run python scripts/inspect_audio.py demo/audio/*.mp3
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audio import decode, loudness  # noqa: E402
from app.config import settings  # noqa: E402


def main(paths: list[str]) -> None:
    header = f"{'file':34} {'dur':>6} {'rate':>6} {'ch':>2} {'peak dBFS':>9} {'gain dB':>8} {'LUFS-I':>7} {'LRA':>5} {'max S':>6} {'dBTP':>6} {'PLR':>5} {'time':>5}"
    print(header)
    print("-" * len(header))
    for name in paths:
        start = time.perf_counter()
        try:
            prepared = decode.prepare(Path(name), settings.analysis_sample_rate,
                                      settings.min_duration_s, settings.max_duration_s)
        except decode.AudioError as e:
            print(f"{Path(name).name[:34]:34} rejected: {e}")
            continue
        if prepared.silent:
            print(f"{Path(name).name[:34]:34} silent: normalisation unavailable")
            continue
        result = loudness.measure(prepared.signal, prepared.sample_rate)
        elapsed = time.perf_counter() - start
        integrated = result.integrated_lufs
        plr = result.max_true_peak_dbtp - integrated if integrated is not None else float("nan")
        print(
            f"{Path(name).name[:34]:34} {prepared.duration_s:6.1f} {prepared.probe.sample_rate:6d} {prepared.channels:2d} "
            f"{prepared.original_peak_dbfs:+9.2f} {prepared.applied_gain_db:+8.2f} {integrated:7.2f} "
            f"{result.lra_lu:5.1f} {np.nanmax(result.short_term_lufs):6.2f} {result.max_true_peak_dbtp:+6.2f} "
            f"{plr:5.2f} {elapsed:4.1f}s"
        )
        if abs(result.max_sample_peak_dbfs) > 0.01:
            print(f"   WARNING: metered sample peak {result.max_sample_peak_dbfs:+.3f} dBFS, expected 0.00")
        for warning in prepared.decode_warnings[:3]:
            print(f"   decoder: {warning}")


if __name__ == "__main__":
    main(sys.argv[1:])
