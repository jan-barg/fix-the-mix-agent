"""The Writer (DESIGN.md §7.4): accepted findings and the Lead's decisions in, a ReportDraft out.

The Writer sees only what the Lead accepted, the evidence behind it and the figures it may show.
Its draft is validated (app.report.validate); problems go back to it once, and whatever still fails
is dropped from the report.
"""

import asyncio
import json
import re
from pathlib import Path

from app import runs, trace
from app.agents import context, llm, researchers, toolbox
from app.agents.schemas import FixedBlocks, ReportDraft
from app.audio.features import clock, clock_events
from app.evidence import Evidence
from app.report import validate
from app.runs import Run

PROMPT = (Path(__file__).parent / "prompts" / "writer.md").read_text()
TIMES = re.compile(r",?\s*\d+:\d{2}–\d+:\d{2}")


def plain_caption(caption: str) -> str:
    """A figure caption without its time ranges (the report shows those under source details), and with
    event times as m:ss ("kick at 1:55")."""
    return clock_events(TIMES.sub("", caption))


# --- What the report may draw on ---


def evidence_ids(run: Run) -> list[str]:
    """Evidence behind the accepted findings: every record a researcher with an accepted finding saw."""
    accepted = run.research["finished"].get("accepted_findings", {})
    tasks = list(dict.fromkeys(f["task_id"] for f in accepted.values()))
    ids: list[str] = []
    for tid in tasks:
        for e in run.research["results"][tid]["evidence"]:
            if e["id"] not in ids:
                ids.append(e["id"])
    return ids


def load_evidence(run: Run) -> dict[str, Evidence]:
    out = {}
    for eid in evidence_ids(run):
        try:
            out[eid] = toolbox.load(run.id, eid)
        except toolbox.ToolError:
            continue
    return out


def figures(evidence: dict[str, Evidence]) -> list[dict]:
    return [{"id": f.id, "title": f.title, "caption": f.caption, "evidence_id": ev.id, "tool": ev.tool,
             "ranges": ev.source.get("ranges", [])}
            for ev in evidence.values() for f in ev.figures]


def report_context(run: Run, evidence: dict[str, Evidence]) -> validate.Context:
    accepted = run.research["finished"].get("accepted_findings", {})
    texts = [" ".join(str(v) for v in f.values() if isinstance(v, str)) for f in accepted.values()]
    return validate.Context(evidence=evidence, figures={f["id"] for f in figures(evidence)},
                            hz=validate.hz_values(texts), facts={t.grid.bpm for t in run.tracks})


# --- The Writer's message ---


def _brief(run: Run) -> str:
    return researchers._brief_text(run)


def _tracks(run: Run) -> str:
    lines = []
    for t in run.tracks:
        sections = "; ".join(f"{s.name} {clock(s.start_s)}–{clock(s.end_s)}" for s in context.named_sections(run, t.id))
        lines.append(f"- {t.display_name}: \"{t.filename}\", {t.grid.bpm:g} BPM, {clock(t.source['duration_s'])}. "
                     f"Sections: {sections}")
    pairs = context.matched_pairs(run)
    if pairs:
        lines.append("Matched sections: " + "; ".join(
            f"{p['user'].split('/', 1)[1]} with " + (", ".join(v for k, v in p.items() if k != "user") or "nothing")
            for p in pairs))
    return "\n".join(lines)


def _decisions(run: Run) -> str:
    fin = run.research["finished"]
    lines = [f"Summary: {fin['summary']}", "Priorities, in order:"]
    for i, p in enumerate(fin["priorities"], 1):
        lines.append(f"{i}. {p['title']}. Why: {p['why']} Cites: {', '.join(p['finding_refs'])}")
    lines.append("Preserve:")
    lines.extend(f"- {x}" for x in fin["preserve"])
    lines.append("Limitations:")
    lines.extend(f"- {x}" for x in fin["limitations"])
    return "\n".join(lines)


def _finding_line(ref: str, f: dict) -> str:
    kind = f["kind"]
    if kind == "observation":
        values = "; ".join(f"{v['name']} = {v['value']:g} {v['unit']} ({v['evidence_id']})" for v in f["values"])
        return f"- {ref} observation: {f['text']}" + (f" [values: {values}]" if values else "")
    if kind == "comparison":
        return f"- {ref} comparison with {f['target']}: {f['text']}"
    if kind == "interpretation":
        alts = "; ".join(f["alternatives"])
        return f"- {ref} interpretation: {f['text']}" + (f" Alternatives: {alts}" if alts else "")
    return f"- {ref} experiment: {f['action']} Verify: {f['how_to_verify']} Serves: {f['supports_goal']}"


def _findings(run: Run) -> str:
    accepted = run.research["finished"].get("accepted_findings", {})
    by_task: dict[str, list[str]] = {}
    for ref, f in accepted.items():
        by_task.setdefault(f["task_id"], []).append(_finding_line(ref, f))
    lines = []
    for tid, found in by_task.items():
        rec = run.research["results"][tid]
        r = rec["result"]
        lines.append(f"### {rec['specialist'].replace('_', ' ')} researcher, round {rec['round']}: {rec['question']}")
        lines.append(f"Scope: {r['scope']}")
        lines.extend(found)
        if r["limitations"]:
            lines.append("Researcher's limitations: " + "; ".join(r["limitations"]))
        lines.append(f"Researcher's confidence: {r['confidence']}")
        lines.append("")
    return "\n".join(lines)


def build_messages(run: Run, evidence: dict[str, Evidence]) -> list[dict]:
    figs = [f"- {f['id']}: {f['title']}. {plain_caption(f['caption'])}" for f in figures(evidence)]
    text = "\n".join([
        "## The brief", _brief(run), "",
        "## The producer's comments", researchers._comments_text(run), "",
        "## Tracks", _tracks(run), "",
        "## The Lead's decisions", _decisions(run), "",
        "## Accepted findings", _findings(run),
        "## Figures you can show", *(figs or ["(none)"]), "",
        "## Measured values", *(ev.summary_text() for ev in evidence.values()),
    ])
    # Not cached: the repair asks for a different schema, which invalidates the cache, so a cache write
    # would only add 25% to this call.
    return [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}]


def repair_message(draft: ReportDraft, problems: dict[str, list[str]]) -> str:
    current = {b.id: b.text for b in validate.blocks(draft)}
    lines = ["The draft did not pass the report checks. priorityN.<field> is a field of the Nth priority, and "
             "paragraphs.N the Nth item of paragraphs (both counted from 1).", ""]
    for block, found in problems.items():
        if block == "report":
            lines.extend(f"- The report: {p}" for p in found)
            continue
        lines.extend(f"- {block}: {p}" for p in found)
        lines.append(f"  Current text: {current.get(block, '')}")
    lines += ["", "Return only the blocks with problems, corrected: add the missing claim with the scalar's exact name "
                  "and value, quote the value the evidence gives, or take the number out. Keep the rest of each text as "
                  "it is. Add new paragraphs only for a required section that is missing."]
    return "\n".join(lines)


def apply_fixes(draft: ReportDraft, fixes: FixedBlocks) -> ReportDraft:
    d = draft.model_copy(deep=True)
    for fix in fixes.blocks:
        head, _, rest = fix.id.partition(".")
        try:
            if head == "paragraphs":
                para = d.paragraphs[int(rest) - 1]
                para.text, para.claims = fix.text, fix.claims
            elif head.startswith("priority") and rest in validate.PRIORITY_FIELDS:
                p = d.priorities[int(head[len("priority"):]) - 1]
                setattr(p, rest, fix.text)
                p.claims = [*p.claims, *(c for c in fix.claims if c not in p.claims)]
        except (ValueError, IndexError):
            continue  # an ID that names nothing: the block keeps its problem
    d.paragraphs.extend(fixes.new_paragraphs)
    return d


# --- Writing ---


async def _call(run_id: str, messages: list[dict], label: str, schema=ReportDraft):
    event = trace.start(run_id, name=label, agent="writer", kind="model_call")
    try:
        out, c = await llm.complete_json("writer", messages, schema)
    except Exception as e:
        trace.end(event, f"{type(e).__name__}: {str(e)[:400]}", status="error")
        raise
    runs.add_usage(run_id, c.input_tokens, c.output_tokens, c.cost_usd)
    summary = (f"{len(out.priorities)} priorities, {len(out.paragraphs)} paragraphs" if schema is ReportDraft
               else f"{len(out.blocks)} blocks fixed, {len(out.new_paragraphs)} paragraphs added")
    trace.end(event, summary, model=c.model, input_tokens=c.input_tokens, output_tokens=c.output_tokens,
              cost_usd=c.cost_usd, seconds=round(c.seconds, 2))
    return out, c.message


async def write(run_id: str) -> tuple[ReportDraft, validate.Context, dict]:
    """Draft, check, repair once, drop what still fails. Returns the draft, its context and a record of
    the checks: {first: problems, final: problems, removed: [...]}."""
    run = runs.get(run_id)
    evidence = await asyncio.to_thread(load_evidence, run)
    ctx = report_context(run, evidence)
    messages = build_messages(run, evidence)
    draft, reply = await _call(run_id, messages, "writer")
    first = validate.check(draft, ctx)
    final = first
    if first:
        messages = [*messages, reply, {"role": "user", "content": repair_message(draft, first)}]
        try:
            fixes, _ = await _call(run_id, messages, "writer:repair", FixedBlocks)
            draft = apply_fixes(draft, fixes)
            final = validate.check(draft, ctx)
        except llm.ModelCallError:
            pass  # keep the first draft; its problems are dropped below
    draft, removed = validate.drop(draft, final, ctx)
    return draft, ctx, {"first": first, "final": final, "removed": removed}


def dump(draft: ReportDraft) -> str:
    return json.dumps(draft.model_dump(), indent=1, ensure_ascii=False)
