"""The Humanizer (DESIGN.md §7.6): edits the report's prose so it reads like a person wrote it.

Its instructions adapt the humanizer skill v3.1.0 (github.com/blader/humanizer, MIT) to the report.
Only prose goes out, block by block. A block comes back only if its protected content is unchanged
(every number, number-and-unit pair, track and section name, and whether it hedges) and it still
passes the report checks; otherwise the Writer's text stays.
"""

import json
import re
from pathlib import Path

from app import runs, trace
from app.agents import context, llm
from app.agents.schemas import EditedBlocks, ReportDraft
from app.config import settings
from app.report import validate
from app.runs import Run

PROMPT = (Path(__file__).parent / "prompts" / "humanizer.md").read_text()
NUMBER = re.compile(r"\d+(?:[.:]\d+)?")
HEDGE = re.compile(r"\b(?:could|may|might|suggests?|likely|possibly|perhaps|probably|appears?|seems?|"
                   r"consistent with)\b", re.I)
TRACKS = re.compile(r"\byour track\b|\breference [1-3]\b", re.I)


def names(run: Run) -> list[str]:
    """Section names to protect ("drop 2", "intro", custom labels), longest first."""
    found = {s.name.lower() for t in run.tracks for s in context.named_sections(run, t.id) if s.label != "unknown"}
    return sorted(found, key=len, reverse=True)


def protected(text: str, section_names: list[str]) -> tuple:
    lowered = text.lower()
    present = set()
    for name in section_names:  # longest first, so "drop 2" is not also counted as "drop"
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        if pattern.search(lowered):
            present.add(name)
            lowered = pattern.sub(" ", lowered)
    return (frozenset(NUMBER.findall(text)),
            frozenset((a, b, u) for a, b, u in validate.WITH_UNIT.findall(text.replace("−", "-"))),
            frozenset(m.lower() for m in TRACKS.findall(text)),
            frozenset(present),
            bool(HEDGE.search(text)))


def _differences(before: tuple, after: tuple) -> list[str]:
    labels = ("numbers", "numbers with units", "track names", "section names", "hedging")
    return [label for label, a, b in zip(labels, before, after) if a != b]


def set_text(draft: ReportDraft, block_id: str, text: str) -> None:
    head, _, rest = block_id.partition(".")
    if head == "paragraphs":
        draft.paragraphs[int(rest) - 1].text = text
    else:
        setattr(draft.priorities[int(head[len("priority"):]) - 1], rest, text)


async def edit(run_id: str, draft: ReportDraft, ctx: validate.Context) -> tuple[ReportDraft, dict]:
    """The edited draft and a record: {status: applied | skipped | failed, edited, reverted: {id: reasons}}."""
    if not settings.humanizer:
        return draft, {"status": "skipped", "reason": "turned off (HUMANIZER=off)"}
    run = runs.get(run_id)
    section_names = names(run)
    blocks = {b.id: b for b in validate.blocks(draft)}
    payload = [{"id": b.id, "kind": b.kind, "text": b.text} for b in blocks.values()]
    messages = [{"role": "system", "content": PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)}]
    event = trace.start(run_id, name="humanizer", agent="humanizer", kind="model_call", args={"blocks": len(payload)})
    try:
        result, c = await llm.complete_json("humanizer", messages, EditedBlocks)
    except Exception as e:
        trace.end(event, f"{type(e).__name__}: {str(e)[:400]}", status="error")
        return draft, {"status": "failed", "reason": f"{type(e).__name__}: {str(e)[:300]}"}
    runs.add_usage(run_id, c.input_tokens, c.output_tokens, c.cost_usd)

    edited = draft.model_copy(deep=True)
    changed, reverted = [], {}
    for item in result.blocks:
        b = blocks.get(item.id)
        new = item.text.strip()
        if b is None or not new or new == b.text:
            continue
        reasons = _differences(protected(b.text, section_names), protected(new, section_names))
        candidate = validate.Block(b.id, b.kind, new, b.claims, b.exempt)
        reasons += validate.check_block(candidate, ctx)
        if reasons:
            reverted[b.id] = reasons
            continue
        set_text(edited, b.id, new)
        changed.append(b.id)
    trace.end(event, f"{len(changed)} blocks edited, {len(reverted)} reverted to the writer's text",
              model=c.model, input_tokens=c.input_tokens, output_tokens=c.output_tokens, cost_usd=c.cost_usd,
              seconds=round(c.seconds, 2))
    return edited, {"status": "applied", "edited": changed, "reverted": reverted}
