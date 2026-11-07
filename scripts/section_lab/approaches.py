"""Section finders on per-bar features (feats.bar_features). Each returns [(start_bar, end_bar, label)] in bar indices
(0 = first bar start), end exclusive; to_sections() turns that into seconds."""
import numpy as np

CLIP = {"pulse": (0, 30), "hat_pulse": (0, 40)}
NOV_KEYS = ["loud", "low", "pulse", "hat", "hat_pulse", "bright", "width", "low_share", "upper"]


def _z(v):
    return (v - v.mean()) / (v.std() + 1e-9)


def prep(F):
    G = {k: (np.clip(F[k], *CLIP[k]) if k in CLIP else F[k]) for k in NOV_KEYS}
    G["shape"] = F["shape"]
    return G


def novelty(F, p):
    """Change between the W bars before and after each bar start: weighted feature distance plus timbre shape."""
    G = prep(F)
    W = int(p.get("win", 4))
    x = np.stack([_z(G[k]) * p.get(f"w_{k}", 1.0) for k in NOV_KEYS], axis=1)
    s = G["shape"]
    n = len(x)
    nov = np.zeros(n)
    for b in range(1, n):
        lo, hi = max(0, b - W), min(n, b + W)
        d = np.linalg.norm(x[lo:b].mean(0) - x[b:hi].mean(0))
        a, c = s[lo:b].mean(0), s[b:hi].mean(0)
        cos = 1 - a @ c / (np.linalg.norm(a) * np.linalg.norm(c) + 1e-9)
        nov[b] = d + p.get("w_shape", 1.0) * 10 * cos
    return nov


def phrase_offset(nov, period=4):
    return int(np.argmax([nov[k::period].sum() for k in range(period)]))


def peaks(nov, p, period=4, off=None):
    off = phrase_offset(nov, period) if off is None else off
    n = len(nov)
    cand = [b for b in range(off if off else period, n - 2, period) if b >= 2]
    if not cand:
        return []
    z = _z(nov[cand]) if len(cand) > 1 else np.ones(1)
    keep = []
    for i, b in enumerate(cand):
        nb = [j for j in range(len(cand)) if abs(cand[j] - b) <= p.get("nms", 4) and j != i]
        if z[i] > p.get("thr", 0.0) and all(z[i] >= z[j] for j in nb):
            keep.append(b)
    return keep


def _sec_stats(F, a, b):
    sl = slice(a, b)
    low = F["low"][sl]
    t = np.arange(len(low))
    slope = float(np.polyfit(t, low, 1)[0]) if len(low) > 2 else 0.0
    return {"loud": float(np.mean(F["loud"][sl])), "low": float(np.mean(low)),
            "pulse": float(np.mean(np.clip(F["pulse"][sl], 0, 30))),
            "hat_pulse": float(np.mean(np.clip(F["hat_pulse"][sl], 0, 40))),
            "hat": float(np.mean(F["hat"][sl])), "slope": slope, "low_end": float(np.mean(low[-2:]))}


def _merge(secs, keep_split=("build",)):
    out = []
    for s in secs:
        if out and out[-1][2] == s[2] and s[2] not in keep_split:
            out[-1] = (out[-1][0], s[1], s[2])
        else:
            out.append(s)
    return out


# --- N: cut at the biggest changes, then label each piece ---

def approach_n(F, p):
    n = len(F["low"])
    nov = novelty(F, p)
    cuts = peaks(nov, p)
    edges = [0, *cuts, n]
    stats = [_sec_stats(F, a, b) for a, b in zip(edges[:-1], edges[1:])]
    mx_low = np.percentile(F["low"], 95)
    mx_loud = np.percentile(F["loud"], 95)
    labels = []
    for i, ((a, b), s) in enumerate(zip(zip(edges[:-1], edges[1:]), stats)):
        prev_low = stats[i - 1]["low_end"] if i else s["low"]
        reentry = s["low"] - prev_low
        full = s["low"] >= mx_low - p["full_db"] and s["loud"] >= mx_loud - p["full_loud"] and s["pulse"] >= p["pulse_min"]
        if full and i > 0 and reentry >= p["jump"] and a >= p["min_start"]:
            labels.append("drop")
        elif s["pulse"] < p["bd_pulse"] and s["hat_pulse"] < p["bd_hat"] or s["low"] < mx_low - p["bd_low"]:
            labels.append("breakdown")
        else:
            labels.append("verse")
    for i in range(len(labels) - 1):
        if labels[i + 1] == "drop" and labels[i] != "drop" and (stats[i]["slope"] < -p["build_slope"]
                                                              or stats[i]["low_end"] < mx_low - p["build_db"]):
            labels[i] = "build"
    if labels[0] != "drop":
        labels[0] = "intro"
    last_drop = max((i for i, l in enumerate(labels) if l == "drop"), default=None)
    if last_drop is not None and last_drop < len(labels) - 1 and labels[-1] != "drop":
        labels[-1] = "outro"
    elif last_drop is None and len(labels) > 1:
        labels[-1] = "outro"
    return _merge([(a, b, l) for (a, b), l in zip(zip(edges[:-1], edges[1:]), labels)])


# --- R: drops first (where the full low end comes back after a dip), then builds, breakdowns, the rest ---

def approach_r(F, p):
    n = len(F["low"])
    low, pulse, loud = F["low"], np.clip(F["pulse"], 0, 30), F["loud"]
    hat_pulse = np.clip(F["hat_pulse"], 0, 40)
    mx_low, mx_loud = np.percentile(low, 95), np.percentile(loud, 95)
    full = (low >= mx_low - p["full_db"]) & (loud >= mx_loud - p["full_loud"]) & (pulse >= p["pulse_min"])
    K = int(p["look"])
    nov = novelty(F, p)
    off = phrase_offset(nov, 4)
    label = np.array([""] * n, dtype=object)
    drops = []
    b = int(p["min_start"])
    while b < n:
        prev = low[max(0, b - K):b]
        if full[b] and prev.size and low[b] - prev.min() >= p["jump"] and np.mean(full[b:b + 4]) >= 0.5:
            # Snap to the 4-bar line within a bar of the jump.
            s = min((x for x in (b - 1, b, b + 1) if (x - off) % 4 == 0 and 0 <= x < n), default=b, key=lambda x: abs(x - b))
            e = s
            gap = 0
            while e < n and e - s < p["max_len"]:
                gap = 0 if low[e] >= mx_low - p["end_db"] else gap + 1
                if gap > p["gap"]:
                    e -= gap - 1
                    break
                e += 1
            length = max(8, int(round((e - s) / 8)) * 8) if p.get("snap8", 1) else max(4, e - s)
            e = min(n, s + length)
            drops.append((s, e))
            label[s:e] = "drop"
            b = e + 1
        else:
            b += 1
    # Builds: the bars before a drop while the low end is thinning out.
    for s, _ in drops:
        best = 0
        for L in (16, 8, 4):
            a = s - L
            if a < 0 or (label[a:s] != "").any():
                continue
            seg = low[a:s]
            slope = np.polyfit(np.arange(L), seg, 1)[0]
            if slope <= -p["build_slope"] or seg[-2:].mean() <= mx_low - p["build_db"]:
                best = L
                break
        if best:
            label[s - best:s] = "build"
    # Breakdowns: no kick pumping and fewer hat spikes, or a much thinner low end.
    bd = (label == "") & (((pulse < p["bd_pulse"]) & (hat_pulse < p["bd_hat"])) | (low < mx_low - p["bd_low"]))
    # smooth to runs of at least 4 bars
    i = 0
    while i < n:
        if bd[i]:
            j = i
            while j < n and bd[j]:
                j += 1
            if j - i >= p["bd_min"]:
                label[i:j] = "breakdown"
            i = j
        else:
            i += 1
    # The rest: split at the strongest changes; first piece intro, after the last drop outro, else verse.
    cuts = set(peaks(nov, p, off=off))
    secs = []
    a = 0
    for b in range(1, n + 1):
        if b == n or label[b] != label[a] or (label[a] == "" and b in cuts):
            secs.append([a, b, label[a] or "verse"])
            a = b
    if secs and secs[0][2] not in ("drop",):
        secs[0][2] = "intro" if secs[0][2] in ("verse", "breakdown") else secs[0][2]
    last = max((k for k, s in enumerate(secs) if s[2] == "drop"), default=None)
    if last is not None:
        for s in secs[last + 1:]:
            if s[2] in ("verse",):
                s[2] = "outro"
    return _merge([tuple(s) for s in secs])


def to_sections(F, bars, duration):
    starts, ends = F["starts"], F["ends"]
    out = []
    for a, b, lab in bars:
        out.append({"label": lab, "start_s": 0.0 if a == 0 else float(starts[a]),
                    "end_s": duration if b >= len(starts) else float(starts[b])})
    return out


# --- V: per-bar scores for each section type, then the best sequence (Viterbi), changes only on 4-bar lines ---

STATES = ["intro", "verse", "build", "drop", "breakdown", "outro"]
ALLOWED = {
    "intro": {"intro", "verse", "build", "breakdown"},
    "verse": {"verse", "build", "drop", "breakdown", "outro"},
    "build": {"build", "drop", "verse", "breakdown"},
    "drop": {"drop", "verse", "breakdown", "build", "outro"},
    "breakdown": {"breakdown", "build", "verse", "drop"},
    "outro": {"outro"},
}


def approach_v(F, p):
    n = len(F["low"])
    low, loud = F["low"], F["loud"]
    pulse = np.clip(F["pulse"], 0, 30)
    hp = np.clip(F["hat_pulse"], 0, 40)
    mx_low = np.percentile(low, 95)
    zl, zd, zp, zh, zhp, zb = (_z(low), _z(loud), _z(pulse), _z(F["hat"]), _z(hp), _z(F["bright"]))
    deficit = np.clip(mx_low - low, 0, 40) / 10  # how far the low end is below its full level, in 10 dB
    slope = np.zeros(n)  # low-end trend over the next 4 bars (negative while it thins out)
    for b in range(n):
        seg = low[b:min(n, b + 4)]
        slope[b] = np.polyfit(np.arange(len(seg)), seg, 1)[0] if len(seg) > 2 else 0.0
    reentry = np.array([max(0.0, low[b] - low[max(0, b - int(p["look"])):b].min()) if b else 0.0 for b in range(n)])
    pos = np.arange(n) / max(n - 1, 1)
    full = np.clip(1 - deficit, 0, 1)
    E = {
        "drop": p["d_low"] * zl + p["d_loud"] * zd + p["d_pulse"] * np.clip(zp, -2, 1) - p["d_kickonly"] * np.clip(zp - 1.5, 0, None),
        "build": p["b_slope"] * np.clip(-slope, 0, 6) / 3 + p["b_def"] * np.clip(deficit, 0, 3) * (deficit < 3) + p["b_bright"] * zb,
        "breakdown": -p["k_pulse"] * zp - p["k_hat"] * zhp + p["k_def"] * deficit - p["k_bias"],
        "verse": np.full(n, p["v_bias"]) + p["v_full"] * full,
        "intro": p["i_bias"] - p["i_pos"] * pos * 10,
        "outro": p["o_bias"] - p["o_pos"] * (1 - pos) * 10,
    }
    off = phrase_offset(novelty(F, p), 4) if p.get("use_nov", 1) else 0
    can_change = np.array([(b - off) % 4 == 0 for b in range(n)])
    S = len(STATES)
    idx = {s: i for i, s in enumerate(STATES)}
    score = np.full((n, S), -1e9)
    back = np.zeros((n, S), dtype=int)
    score[0, idx["intro"]] = E["intro"][0]
    score[0, idx["verse"]] = E["verse"][0] - p["start_verse"]
    for b in range(1, n):
        for j, s in enumerate(STATES):
            best, arg = -1e9, j
            for i, r in enumerate(STATES):
                if score[b - 1, i] <= -1e8:
                    continue
                if r == s:
                    v = score[b - 1, i]
                elif can_change[b] and s in ALLOWED[r]:
                    v = score[b - 1, i] - p["switch"]
                    if s == "drop":
                        v += p["r_bonus"] * reentry[b] / 10 - (p["drop_cold"] if r not in ("build", "breakdown") else 0)
                else:
                    continue
                if v > best:
                    best, arg = v, i
            score[b, j] = best + E[s][b]
            back[b, j] = arg
    j = int(np.argmax(score[-1]))
    path = [j]
    for b in range(n - 1, 0, -1):
        j = back[b, j]
        path.append(j)
    path = path[::-1]
    secs, a = [], 0
    for b in range(1, n + 1):
        if b == n or path[b] != path[a]:
            secs.append((a, b, STATES[path[a]]))
            a = b
    return secs
