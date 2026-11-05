"""Specialist researchers (DESIGN.md §7.2, §7.3).

A researcher runs in a fresh context: the harness measures its default evidence bundle (plus any
evidence the Lead passes), sends the summaries and figures with the task and brief, and validates
the reply as a ResearcherResult. Every number the researcher quotes is checked against the evidence
before the Lead sees the result; mismatches become warnings, not silent corrections.
"""

import asyncio
import base64
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from app import runs, trace
from app.agents import context, llm, toolbox
from app.agents.schemas import ResearcherResult
from app.evidence import Evidence
from app.runs import Run

PROMPTS = Path(__file__).parent / "prompts"
MAX_FIGURES = 10  # images per researcher call
BUNDLE_BARS = 4  # length of the spectrogram passage in the low-end bundle

FINDING_KINDS = {"observations": "o", "comparisons": "c", "interpretations": "i", "experiments": "x"}


@cache
def system_prompt(specialist: str) -> str:
    return (PROMPTS / "researcher_base.md").read_text() + "\n\n" + (PROMPTS / f"{specialist}.md").read_text()


@dataclass
class Task:
    task_id: str  # the Lead's run_researcher call in the trace; finding refs are "<task_id>/o1"
    specialist: str
    question: str
    user_intent: str
    scopes: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)


# --- Evidence bundles ---


def _track_order(run: Run, ref: str) -> int:
    ids = [t.id for t in run.tracks]
    tid = context.track_of(ref)
    return ids.index(tid) if tid in ids else len(ids)


def plan_scopes(run: Run, scopes: list[str]) -> tuple[list[str], list[str]]:
    """(tracks, focus): the tracks to compare whole, and the passages to focus on, your track first.
    Passages the Lead names replace the default focus; whole-track references narrow the track set."""
    focus = [s.strip() for s in scopes if "/" in s or "@" in s] or context.default_focus(run)
    named = list(dict.fromkeys(context.track_of(s) for s in scopes))
    tracks = [t.id for t in run.tracks if not named or t.id in named or t.id == "user"]
    return tracks, sorted(focus, key=lambda r: _track_order(run, r))


def _combine(run: Run, refs: list[str]) -> list[str]:
    """The references to average into one target, when the brief asks for an averaged comparison."""
    brief = run.brief or {}
    target = brief.get("combined_target") or {}
    if brief.get("comparison_mode") != "average":
        return []
    contributors = set(target.get("contributors") or [t.id for t in run.tracks if t.role == "reference"])
    combine = [r for r in refs if context.track_of(r) in contributors]
    return combine if len(combine) > 1 else []


def _best_kick(ev: Evidence) -> float | None:
    table = ev.results.get("tables", {}).get("candidates", [])
    clean = [c for c in table if not c["flags"]]
    best = (clean or table or [None])[0]
    return None if best is None else best["time_s"]


def gather(tb: toolbox.Toolbox, task: Task, parent: str) -> tuple[list[Evidence], list[str]]:
    """Measure the specialist's default bundle and load the evidence the Lead passed. Blocking."""
    run = runs.get(tb.run.id)
    tracks, focus = plan_scopes(run, task.scopes)
    evidence: list[Evidence] = []
    notes: list[str] = []

    def measure(name: str, args: dict) -> Evidence | None:
        try:
            ev = tb.measure(name, args, agent=f"{task.specialist} researcher", parent_task_id=parent)
        except toolbox.ToolError as e:
            notes.append(f"{name} could not run: {e}")
            return None
        if all(e.id != ev.id for e in evidence):
            evidence.append(ev)
        return ev

    sections = [r for r in focus if "/" in r or "@" in r]
    if task.specialist == "kick":
        picks = []
        for tid in tracks:
            within = [r for r in sections if context.track_of(r) == tid]
            ev = measure("find_kick_candidates", {"track": tid, "within": within, "n": 3})
            if ev is not None and _best_kick(ev) is None and within:
                ev = measure("find_kick_candidates", {"track": tid, "n": 3})
            if ev is not None and (t := _best_kick(ev)) is not None:
                picks.append({"track": tid, "time_s": t})
        if picks:
            measure("analyse_kick", {"kicks": picks})
    elif task.specialist == "low_end":
        measure("measure_low_end", {"scopes": sections or tracks})
        passages = []
        for ref in sections:
            try:
                scope = context.resolve(run, ref, tb.tracks())
            except context.ScopeError:
                continue  # measure_low_end has already noted the bad reference
            length = BUNDLE_BARS * 4 * scope.track.features.grid.beat_s
            end = min(scope.start_s + length, scope.end_s)
            if end - scope.start_s >= 1.0:
                passages.append(f"{scope.track.id}@{scope.start_s:.2f}-{end:.2f}")
        if passages:
            measure("render_spectrogram", {"scopes": passages, "preset": "overview"})
    elif task.specialist == "tonal_loudness":
        measure("measure_loudness", {"scopes": tracks})
        if sections:
            measure("measure_loudness", {"scopes": sections})
        user_sections = [s.ref for s in context.named_sections(run, "user") if s.label != "unknown"]
        if len(user_sections) > 1:
            measure("measure_loudness", {"scopes": user_sections})
        measure("measure_tonal_balance", {"scopes": tracks, "combine": _combine(run, tracks)})
        if sections:
            measure("measure_tonal_balance", {"scopes": sections, "combine": _combine(run, sections)})
    elif task.specialist == "stereo":
        measure("measure_stereo", {"scopes": tracks})
        if sections:
            measure("measure_stereo", {"scopes": sections})

    for evidence_id in task.evidence_ids:
        if any(e.id == evidence_id for e in evidence):
            continue
        try:
            evidence.append(toolbox.load(run.id, evidence_id))
        except toolbox.ToolError as e:
            notes.append(str(e))
    return evidence, notes


# --- The researcher's message ---


def _brief_text(run: Run) -> str:
    brief = run.brief or {}
    names = {t.id: t.display_name for t in run.tracks}
    lines = [f"Goals: {'; '.join(brief.get('goals') or []) or 'a general comparison with the references'}",
             f"Preserve: {'; '.join(brief.get('preserve') or []) or 'nothing stated'}",
             f"Stage: {brief.get('production_stage', 'unknown')}",
             f"Comparison mode: {brief.get('comparison_mode', 'separate')}"]
    if target := brief.get("combined_target"):
        lines.append(f"Combined target: {target.get('method')} from {', '.join(names.get(c, c) for c in target.get('contributors', []))} "
                     f"(aspects: {', '.join(target.get('aspects', [])) or 'all'})")
    for role in brief.get("reference_roles") or []:
        aspects = ", ".join(role.get("aspects") or []) or "general comparison"
        excl = ", ".join(role.get("exclusions") or [])
        lines.append(f"{names.get(role['track_id'], role['track_id'])}: use for {aspects}" + (f"; not a target for {excl}" if excl else ""))
    lines.extend(f"Assumption: {a}" for a in brief.get("assumptions") or [])
    return "\n".join(lines)


def _comments_text(run: Run) -> str:
    lines = []
    if (g := (run.comments or {}).get("global", "").strip()):
        lines.append(f"Global: {g}")
    lines.extend(f"{t.display_name}: {t.comment.strip()}" for t in run.tracks if t.comment.strip())
    return "\n".join(lines) or "(none)"


def build_messages(run: Run, task: Task, evidence: list[Evidence], notes: list[str]) -> list[dict]:
    tracks, focus = plan_scopes(run, task.scopes)
    by_ref = {s.ref: s for t in run.tracks for s in context.named_sections(run, t.id)}
    track_lines = [f"- {t.display_name} ({t.id}): {t.grid.bpm:g} BPM" for t in run.tracks if t.id in tracks]
    focus_lines = [f"- {by_ref[r].describe()}" if r in by_ref else f"- {r}" for r in focus] or ["- (no matched sections; whole tracks)"]
    text = "\n".join([
        "## Task", f"Question: {task.question}", f"Serves: {task.user_intent}", "",
        "Tracks:", *track_lines, "", "Focus passages:", *focus_lines, "",
        "## Brief", _brief_text(run), "",
        "## The producer's comments", _comments_text(run), "",
        *(["## Notes", *[f"- {n}" for n in notes], ""] if notes else []),
        "## Evidence",
    ])
    content: list[dict] = [{"type": "text", "text": text}]
    shown = 0
    for ev in evidence:
        content.append({"type": "text", "text": ev.summary_text()})
        for fig in ev.figures:
            if shown >= MAX_FIGURES:
                break
            shown += 1
            content.append({"type": "text", "text": f"Figure: {fig.title}. {fig.caption}"})
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + base64.b64encode(fig.png).decode()}})
    return [{"role": "system", "content": system_prompt(task.specialist)}, {"role": "user", "content": content}]


# --- Checking the reply ---

_NUMBER = re.compile(r"(?<![\w.])[-−]?\d+(?:\.\d+)?")
_WITH_UNIT = re.compile(r"(?<![\w.])([-−]?\d+(?:\.\d+)?)\s*(dBTP|dBFS|dB|LUFS|LU|kHz|Hz|ms|%|beats?|BPM)\b")


def _decimals(x: float) -> int:
    text = repr(float(x))
    return 0 if text.endswith(".0") or "." not in text else len(text.split(".")[1])


def _close(quoted: float, measured: float) -> bool:
    """Equal after rounding to the quoted precision; the sign may be dropped ("3 dB quieter")."""
    tol = 0.5 * 10 ** -_decimals(quoted) + 1e-9
    return abs(quoted - measured) <= tol or abs(abs(quoted) - abs(measured)) <= tol


def _label_numbers(evidence: dict[str, Evidence]) -> set[float]:
    """Numbers that name things rather than measure them: band edges, windows, thresholds."""
    found = set()
    for ev in evidence.values():
        text = " ".join([*ev.results.get("scalars", {}), *map(str, ev.method.values()), ev.source["description"]])
        found.update(float(n.replace("−", "-")) for n in _NUMBER.findall(text))
    return {abs(n) for n in found}


def check(result: ResearcherResult, evidence: dict[str, Evidence]) -> list[str]:
    """Warnings for quoted values that do not match the cited scalars, and for numbers in observations,
    comparisons and interpretations that no quoted value backs."""
    warnings = []
    quoted: list[float] = []
    for i, obs in enumerate(result.observations, 1):
        for v in obs.values:
            ev = evidence.get(v.evidence_id)
            if ev is None:
                warnings.append(f"o{i}: cites evidence {v.evidence_id} that the researcher was not given")
                continue
            scalars = ev.results.get("scalars", {})
            s = scalars.get(v.name) or scalars.get(v.name.strip())
            if s is None:
                warnings.append(f"o{i}: {v.evidence_id} has no scalar named {v.name!r}")
            elif not isinstance(s["value"], (int, float)) or isinstance(s["value"], bool):
                warnings.append(f"o{i}: {v.name!r} is {'unavailable' if s['value'] is None else repr(s['value'])}, "
                                f"not a number to quote")
            elif not _close(v.value, s["value"]):
                warnings.append(f"o{i}: quotes {v.value:g} for {v.name!r}, but the evidence says {s['value']:g} {s['unit']}")
            else:
                quoted.append(v.value)
    labels = _label_numbers(evidence)

    def unbacked(text: str, backing: list[float]) -> list[str]:
        out = []
        for number, unit in _WITH_UNIT.findall(text):
            n = float(number.replace("−", "-")) * (1000 if unit == "kHz" else 1)
            if abs(n) not in labels and not any(_close(float(number.replace("−", "-")), q) or _close(n, q) for q in backing):
                out.append(f"{number} {unit}")
        return out

    for i, obs in enumerate(result.observations, 1):
        own = [v.value for v in obs.values]
        if bad := unbacked(obs.text, own):
            warnings.append(f"o{i}: states {', '.join(bad)} without a matching quoted value")
    # Experiments are left out: their numbers are suggested settings ("cut 2 dB at 200 Hz"), not claims.
    for kind, prefix in (("comparisons", "c"), ("interpretations", "i")):
        for i, item in enumerate(getattr(result, kind), 1):
            text = " ".join(str(v) for v in item.model_dump().values() if isinstance(v, str))
            if bad := unbacked(text, quoted):
                warnings.append(f"{prefix}{i}: states {', '.join(bad)}, which no observation quotes")
    return warnings


def with_ids(result: ResearcherResult) -> dict:
    """The result as stored, with finding IDs (o1, c1, i1, x1) the Lead accepts or rejects by."""
    data = result.model_dump()
    for kind, prefix in FINDING_KINDS.items():
        data[kind] = [{"id": f"{prefix}{i}", **item} for i, item in enumerate(data[kind], 1)]
    return data


def for_lead(record: dict) -> str:
    """What the Lead reads back from run_researcher."""
    tid, r = record["task_id"], record["result"]
    names = {e["id"]: e["description"] for e in record["evidence"]}
    lines = [f"Researcher {tid} ({record['specialist']}): {r['question']}", f"Scope: {r['scope']}",
             "Evidence: " + "; ".join(f"{eid} ({desc})" for eid, desc in names.items())]
    if r["observations"]:
        lines.append("Observations:")
        for o in r["observations"]:
            values = "; ".join(f"{v['name']} = {v['value']:g} {v['unit']}" for v in o["values"])
            lines.append(f"  {tid}/{o['id']}: {o['text']}" + (f" [{values}]" if values else ""))
    if r["comparisons"]:
        lines.append("Comparisons:")
        lines.extend(f"  {tid}/{c['id']} (vs {c['target']}): {c['text']}" for c in r["comparisons"])
    if r["interpretations"]:
        lines.append("Interpretations:")
        lines.extend(f"  {tid}/{i['id']}: {i['text']}" + (f" Alternatives: {'; '.join(i['alternatives'])}" if i["alternatives"] else "")
                     for i in r["interpretations"])
    if r["experiments"]:
        lines.append("Experiments:")
        lines.extend(f"  {tid}/{x['id']}: {x['action']} Verify: {x['how_to_verify']} Serves: {x['supports_goal']}"
                     for x in r["experiments"])
    for label, key in (("Preserve", "preserve"), ("Limitations", "limitations")):
        if r[key]:
            lines.append(f"{label}: " + "; ".join(r[key]))
    lines.append(f"Confidence: {r['confidence']}")
    for f in r["follow_up_requests"]:
        lines.append(f"Follow-up request: {f['need']} Why: {f['why']} ({f['suggested_tool']} on {', '.join(f['suggested_scopes']) or 'unspecified scopes'})")
    if record["warnings"]:
        lines.append("CHECK WARNINGS (numbers that do not match the evidence; reject or re-request these findings):")
        lines.extend(f"  {w}" for w in record["warnings"])
    if record["notes"]:
        lines.append("Harness notes: " + "; ".join(record["notes"]))
    return "\n".join(lines)


# --- Running one researcher ---


async def run_task(run_id: str, task: Task) -> dict:
    """Gather the evidence, call the researcher and store the checked result under research.results."""
    tb = toolbox.Toolbox(runs.get(run_id))
    evidence, notes = await asyncio.to_thread(gather, tb, task, task.task_id)
    if not evidence:
        raise toolbox.ToolError("No evidence could be measured for this task. " + " ".join(notes))
    run = runs.get(run_id)
    messages = build_messages(run, task, evidence, notes)
    event = trace.start(run_id, name=f"researcher:{task.specialist}", agent=f"{task.specialist} researcher",
                        kind="model_call", args={"question": task.question, "evidence_ids": [e.id for e in evidence]},
                        parent_task_id=task.task_id)
    try:
        result, completion = await llm.complete_json("researcher", messages, ResearcherResult)
    except Exception as e:
        trace.end(event, f"{type(e).__name__}: {e}", status="error")
        raise
    runs.add_usage(run_id, completion.input_tokens, completion.output_tokens, completion.cost_usd)
    warnings = check(result, {e.id: e for e in evidence})
    record = {
        "task_id": task.task_id, "specialist": task.specialist, "question": task.question,
        "user_intent": task.user_intent, "scopes": task.scopes, "round": trace.current_round.get(),
        "evidence": [{"id": e.id, "tool": e.tool, "description": e.source["description"]} for e in evidence],
        "result": with_ids(result), "warnings": warnings, "notes": notes,
    }
    with runs.lock(run_id):
        live = runs.get(run_id)
        live.research["results"][task.task_id] = record
        runs.save(live)
    trace.end(event, f"{len(result.observations)} observations, {len(result.experiments)} experiments, "
                     f"{len(result.follow_up_requests)} follow-up requests, {len(warnings)} check warnings",
              model=completion.model, input_tokens=completion.input_tokens, output_tokens=completion.output_tokens,
              cost_usd=completion.cost_usd, seconds=round(completion.seconds, 2))
    return record
