"""Section-finder lab (DESIGN.md §5.3, §12): score approaches against the producer's hand-made maps, hill-climb their
settings, and check them leave-one-track-out.

    PYTHONPATH=. SECTION_AUDIO=~/folder uv run python scripts/section_lab/run.py app        # the app's finder
    PYTHONPATH=. SECTION_AUDIO=~/folder uv run python scripts/section_lab/run.py R default  # an approach, untuned
    PYTHONPATH=. ... run.py R climb 1500   # hill-climb on all 8 tracks (writes .data/section_lab/best_R.json)
    PYTHONPATH=. ... run.py R loo 800      # tune on 7, score the 8th, for each track

Approaches (approaches.py): N cuts at the biggest changes, then labels; R finds drops first (the full low end
coming back after a dip), then builds, breakdowns and the rest; V scores each bar for each section type and takes
the best sequence (Viterbi). The app uses R with the settings in app/audio/sections.py.
"""
import json
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approaches  # noqa: E402
import data  # noqa: E402
import score  # noqa: E402

COMMON = {"win": (4, [2, 4, 6, 8]), "thr": (0.3, (-0.5, 2.0)), "nms": (4, [4, 8]), "w_shape": (1.0, (0, 3)),
          **{f"w_{k}": (1.0, (0, 3)) for k in approaches.NOV_KEYS},
          "full_db": (4.0, (1, 12)), "full_loud": (3.0, (1, 10)), "pulse_min": (3.0, (0, 12)),
          "jump": (6.0, (1, 25)), "min_start": (16, [0, 4, 8, 12, 16, 20, 24]),
          "bd_pulse": (3.0, (0, 12)), "bd_hat": (8.0, (0, 25)), "bd_low": (12.0, (4, 40)),
          "build_slope": (0.8, (0, 5)), "build_db": (8.0, (2, 30))}
SPACES = {
    "N": dict(COMMON),
    "R": {**COMMON, "look": (8, [2, 4, 6, 8, 12, 16]), "end_db": (6.0, (1, 15)), "gap": (2, [0, 1, 2, 3, 4]),
          "max_len": (32, [8, 16, 24, 32, 40]), "snap8": (1, [0, 1]), "bd_min": (4, [2, 4, 6, 8])},
    "V": {"win": (4, [2, 4, 6, 8]), "w_shape": (1.0, (0, 3)), **{f"w_{k}": (1.0, (0, 3)) for k in approaches.NOV_KEYS},
          "look": (8, [2, 4, 6, 8, 12, 16]), "use_nov": (1, [0, 1]),
          "d_low": (1.0, (0, 4)), "d_loud": (1.0, (0, 4)), "d_pulse": (0.5, (0, 4)), "d_kickonly": (0.5, (0, 4)),
          "b_slope": (1.0, (0, 4)), "b_def": (0.5, (0, 4)), "b_bright": (0.2, (0, 2)),
          "k_pulse": (0.8, (0, 4)), "k_hat": (0.8, (0, 4)), "k_def": (0.5, (0, 4)), "k_bias": (0.5, (-2, 4)),
          "v_bias": (0.0, (-2, 2)), "v_full": (0.5, (0, 3)), "i_bias": (0.5, (-2, 4)), "i_pos": (1.0, (0, 5)),
          "o_bias": (0.5, (-2, 4)), "o_pos": (1.0, (0, 5)), "start_verse": (1.0, (0, 5)),
          "switch": (4.0, (0, 20)), "r_bonus": (3.0, (0, 15)), "drop_cold": (2.0, (0, 15))},
}
FN = {"N": approaches.approach_n, "R": approaches.approach_r, "V": approaches.approach_v}


def evaluate(kind, p, tracks):
    rows = []
    for t in tracks:
        pred = approaches.to_sections(t["F"], FN[kind](t["F"], p), t["dur"])
        rows.append(score.score(pred, t["sections"], t["dur"], t["bar"]))
    return score.summary(rows)["score"], rows


def mutate(space, p, rng):
    q = dict(p)
    k = rng.choice(list(space))
    _, dom = space[k]
    if isinstance(dom, list):
        q[k] = rng.choice(dom)
    else:
        lo, hi = dom
        q[k] = float(np.clip(q[k] + rng.gauss(0, (hi - lo) * 0.15), lo, hi))
    return q


def climb(kind, tracks, steps=600, restarts=4, seed=0):
    """Hill-climbing with random restarts: change one or two settings at a time, keep the change if the score
    does not drop."""
    space = SPACES[kind]
    rng = random.Random(seed)
    best_p, best_s = None, -1.0
    for r in range(restarts):
        p = {k: v[0] for k, v in space.items()}
        for _ in range(8 if r else 0):
            p = mutate(space, p, rng)
        s, _ = evaluate(kind, p, tracks)
        for _ in range(steps):
            q = mutate(space, p, rng)
            if rng.random() < 0.3:
                q = mutate(space, q, rng)
            sq, _ = evaluate(kind, q, tracks)
            if sq >= s:
                p, s = q, sq
        if s > best_s:
            best_p, best_s = p, s
    return best_p, best_s


def report(name, tracks, rows):
    for t, r in zip(tracks, rows):
        print(f"{t['name']:26s} {r['score']:.2f}  drop time {r['drop_time_f1']:.2f}  drops {r['drop_seg_f1']:.2f}  "
              f"boundaries {r['boundary_f1']:.2f}  labels {r['label_acc']:.2f}  found {r['drops_found']}  "
              f"right {r['drops_right']}")
    print(name, {k: round(v, 3) for k, v in score.summary(rows).items()})


def main():
    tracks = data.load()
    kind, mode = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "default"
    if kind == "app":
        from app.audio import sections  # noqa: PLC0415
        rows = [score.score([s.to_dict() for s in sections.propose(t["track"])], t["sections"], t["dur"], t["bar"])
                for t in tracks]
        return report("app", tracks, rows)
    steps = int(sys.argv[3]) if len(sys.argv) > 3 else 600
    if mode == "loo":
        held = []
        for i, t in enumerate(tracks):
            p, _ = climb(kind, tracks[:i] + tracks[i + 1:], steps=steps, restarts=2, seed=i)
            held.append(evaluate(kind, p, [t])[1][0])
        return report(f"{kind} leave-one-out", tracks, held)
    p = {k: v[0] for k, v in SPACES[kind].items()}
    if mode == "climb":
        p, _ = climb(kind, tracks, steps=steps)
        (data.CACHE / f"best_{kind}.json").write_text(json.dumps(p, indent=1))
    report(f"{kind} {mode}", tracks, evaluate(kind, p, tracks)[1])


if __name__ == "__main__":
    main()
