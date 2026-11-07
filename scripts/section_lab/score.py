"""Scores a proposed section map against the producer's map. Drops matter most."""
import numpy as np

COARSE = {"drop": "drop", "build": "build", "breakdown": "low", "bridge": "low", "transition": "low",
          "intro": "intro", "outro": "outro"}  # anything else (verse, chorus, hands up, unknown) is "groove"
STEP = 0.25  # s, for time-based scores


def coarse(label: str) -> str:
    word = label.lower().split()[0].rstrip(",")
    return COARSE.get(word, "groove")


def frames(sections, duration):
    t = np.arange(0, duration, STEP)
    lab = np.array(["groove"] * len(t), dtype=object)
    for s in sections:
        lab[(t >= s["start_s"]) & (t < s["end_s"])] = coarse(s["label"])
    return lab


def f1(p, r):
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def score(pred, truth, duration, bar_s) -> dict:
    fp, ft = frames(pred, duration), frames(truth, duration)
    pd, td = fp == "drop", ft == "drop"
    inter = (pd & td).sum()
    drop_p = inter / pd.sum() if pd.sum() else (1.0 if not td.sum() else 0.0)
    drop_r = inter / td.sum() if td.sum() else 1.0
    # Segment level: a true drop is found when a proposed drop covers half of it, and vice versa.
    def covered(a, b):  # share of section a covered by drop time in the other map
        mask = np.zeros_like(pd)
        return a

    def segs(secs):
        return [(s["start_s"], s["end_s"]) for s in secs if coarse(s["label"]) == "drop"]

    def share(seg, other_mask):
        t = np.arange(0, duration, STEP)
        m = (t >= seg[0]) & (t < seg[1])
        return other_mask[m].mean() if m.any() else 0.0

    tsegs, psegs = segs(truth), segs(pred)
    found = sum(share(s, pd) >= 0.5 for s in tsegs)
    right = sum(share(s, td) >= 0.5 for s in psegs)
    seg_r = found / len(tsegs) if tsegs else 1.0
    seg_p = right / len(psegs) if psegs else (1.0 if not tsegs else 0.0)
    pb = [s["start_s"] for s in pred[1:]]
    tb = [s["start_s"] for s in truth[1:]]
    hit_p = sum(any(abs(a - b) <= bar_s for b in tb) for a in pb)
    hit_t = sum(any(abs(a - b) <= bar_s for a in pb) for b in tb)
    bnd_p = hit_p / len(pb) if pb else 0.0
    bnd_r = hit_t / len(tb) if tb else 1.0
    out = {
        "drop_time_f1": f1(drop_p, drop_r),
        "drop_seg_f1": f1(seg_p, seg_r),
        "boundary_f1": f1(bnd_p, bnd_r),
        "label_acc": float((fp == ft).mean()),
        "drops_found": f"{found}/{len(tsegs)}",
        "drops_right": f"{right}/{len(psegs)}",
    }
    out["score"] = 0.45 * out["drop_time_f1"] + 0.2 * out["drop_seg_f1"] + 0.2 * out["boundary_f1"] + 0.15 * out["label_acc"]
    return out


def summary(rows: list[dict]) -> dict:
    keys = ["score", "drop_time_f1", "drop_seg_f1", "boundary_f1", "label_acc"]
    return {k: float(np.mean([r[k] for r in rows])) for k in keys}
