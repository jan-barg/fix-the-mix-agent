"""Report checks before rendering (DESIGN.md §7.5): numbers, figure links, internal IDs, required sections.

A number with a unit in the prose needs a claim in the same block whose value matches the cited
scalar after rounding. Exempt: numbers that name things (band edges, windows) in the evidence used,
the tracks' tempos, and frequencies that an accepted finding states (positions on the frequency axis
that researchers read from spectra). Experiments are exempt as a whole: their numbers are suggested
settings ("cut 2 dB at 200 Hz"), not claims.
"""

import re
from dataclasses import dataclass, field

from app.agents.researchers import _close, _label_numbers
from app.agents.schemas import Claim, ReportDraft
from app.evidence import Evidence

REQUIRED = ("overview", "general", "kick_low_end", "stereo", "section_by_section", "method_notes")
PRIORITY_FIELDS = ("title", "why", "observation", "interpretation", "experiment", "verification")
EXEMPT_FIELDS = {"experiment"}
MAX_PRIORITIES = 3

_NUM = r"[-−]?\d+(?:\.\d+)?"
WITH_UNIT = re.compile(rf"(?<![\w.:])({_NUM})(?:\s*(?:–|-|to)\s*({_NUM}))?\s*(dBTP|dBFS|dB|LUFS|LU|kHz|Hz|ms|%|beats?|BPM)\b")
INTERNAL_ID = re.compile(r"\b(?:ev|fig|trk|sec)_[0-9a-z]{4,}\b|\bt_\d+(?:/[ocix]\d+)?\b")


@dataclass
class Context:
    """What the report may cite: the evidence behind the accepted findings and its figures."""

    evidence: dict[str, Evidence]
    figures: set[str]
    hz: set[float] = field(default_factory=set)  # frequencies stated by accepted findings
    facts: set[float] = field(default_factory=set)  # tempos
    labels: set[float] = field(init=False)

    def __post_init__(self):
        self.labels = _label_numbers(self.evidence)


@dataclass
class Block:
    """One piece of prose: its ID ("priority1.why", or "paragraphs.4" for the fourth paragraph), where it
    sits (a priority field or the paragraph's section), its text and claims."""

    id: str
    kind: str
    text: str
    claims: list[Claim]
    exempt: bool = False


def blocks(draft: ReportDraft) -> list[Block]:
    out = []
    for i, p in enumerate(draft.priorities, 1):
        out.extend(Block(f"priority{i}.{f}", f"priority.{f}", getattr(p, f), p.claims, f in EXEMPT_FIELDS)
                   for f in PRIORITY_FIELDS if getattr(p, f).strip())
    out.extend(Block(f"paragraphs.{i}", para.section, para.text, para.claims) for i, para in enumerate(draft.paragraphs, 1))
    return out


def hz_values(texts: list[str]) -> set[float]:
    found = set()
    for text in texts:
        for a, b, unit in WITH_UNIT.findall(text):
            if unit in ("Hz", "kHz"):
                scale = 1000 if unit == "kHz" else 1
                found.update(abs(float(n.replace("−", "-"))) * scale for n in (a, b) if n)
    return found


def _claim_problem(c: Claim, ctx: Context) -> str | None:
    ev = ctx.evidence.get(c.evidence_id)
    if ev is None:
        return f"a claim cites evidence {c.evidence_id} that is not in the report's evidence"
    s = ev.results.get("scalars", {}).get(c.name)
    if s is None:
        return f"{c.evidence_id} has no value named {c.name!r}"
    if not isinstance(s["value"], (int, float)) or isinstance(s["value"], bool):
        return f"{c.name!r} is unavailable, not a number to quote"
    if not _close(c.value, s["value"]):
        return f"a claim gives {c.value:g} for {c.name!r}, but the evidence says {s['value']:g} {s['unit']}"
    return None


def _backed(n: float, unit: str, claims: list[Claim], ctx: Context) -> bool:
    hz = n * 1000 if unit == "kHz" else n
    if abs(n) in ctx.labels or abs(hz) in ctx.labels:
        return True
    if unit in ("Hz", "kHz") and any(abs(abs(hz) - f) < 0.5 for f in ctx.hz):
        return True
    if unit == "BPM" and any(_close(n, f) for f in ctx.facts):
        return True
    return any(_close(n, c.value) or _close(hz, c.value) for c in claims)


def check_block(b: Block, ctx: Context) -> list[str]:
    problems = []
    if INTERNAL_ID.search(b.text):
        problems.append(f"shows an internal identifier ({INTERNAL_ID.search(b.text).group(0)})")
    if b.exempt:
        return problems
    valid, wrong = [], []
    for c in b.claims:
        if (p := _claim_problem(c, ctx)) is None:
            valid.append(c)
        else:
            wrong.append(p)
    unbacked = []
    for a, rest, unit in WITH_UNIT.findall(b.text):
        for number in (a, rest):
            if number and not _backed(float(number.replace("−", "-")), unit, valid, ctx):
                unbacked.append(f"{number} {unit}")
    if unbacked:  # a wrong claim only matters when the text relies on it
        problems.append(f"states {', '.join(dict.fromkeys(unbacked))} without a claim that matches the evidence"
                        + (f" ({'; '.join(wrong)})" if wrong else ""))
    return problems


def check(draft: ReportDraft, ctx: Context) -> dict[str, list[str]]:
    """Problems by block ID ("report" for the report as a whole). Empty when the draft passes."""
    problems: dict[str, list[str]] = {}
    for b in blocks(draft):
        if found := check_block(b, ctx):
            problems[b.id] = found
    where = [(f"priority {i}", p.figure_ids) for i, p in enumerate(draft.priorities, 1)]
    where += [(f"paragraph {i}", para.figure_ids) for i, para in enumerate(draft.paragraphs, 1)]
    whole = [f"{w} shows figures that do not exist: {', '.join(bad)}" for w, ids in where
             if (bad := [f for f in ids if f not in ctx.figures])]
    if len(draft.priorities) > MAX_PRIORITIES:
        whole.append(f"{len(draft.priorities)} priorities; at most {MAX_PRIORITIES}")
    whole.extend(f"no {name.replace('_', ' ')} paragraph" for name in REQUIRED if not draft.section(name))
    if whole:
        problems["report"] = whole
    return problems


def drop(draft: ReportDraft, problems: dict[str, list[str]], ctx: Context) -> tuple[ReportDraft, list[str]]:
    """Remove what still fails after repair. Returns the cleaned draft and what was removed, in words."""
    d = draft.model_copy(deep=True)
    removed = []
    if len(d.priorities) > MAX_PRIORITIES:
        removed.append(f"{len(d.priorities) - MAX_PRIORITIES} priorities beyond the first {MAX_PRIORITIES}")
    keep = []
    for i, p in enumerate(d.priorities[:MAX_PRIORITIES], 1):
        failing = {f for f in PRIORITY_FIELDS if f"priority{i}.{f}" in problems}
        if failing & {"title", "why", "observation", "experiment"}:
            removed.append(f"priority {i} ({p.title})")
            continue
        for f in sorted(failing):  # interpretation or verification: the priority stands without it
            setattr(p, f, "")
            removed.append(f"the {f} of priority {i}")
        keep.append(p)
    d.priorities = keep
    kept = []
    for i, para in enumerate(d.paragraphs, 1):
        if f"paragraphs.{i}" in problems:
            removed.append(f"a paragraph of {para.section.replace('_', ' ')}")
        else:
            kept.append(para)
    d.paragraphs = kept
    for p in [*d.priorities, *d.paragraphs]:
        p.figure_ids = [f for f in p.figure_ids if f in ctx.figures]
    return d, removed
