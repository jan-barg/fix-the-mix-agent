"""From finished research to the report (SPEC §8.3, DESIGN.md §7.4-§7.6, §10).

Writer (checked, one repair, failing blocks dropped) → Humanizer (block edits that keep protected
content) → final check → HTML → PDF. The run ends Complete, or Partial when a priority or a required
section was lost, or the PDF failed. A failure keeps whatever was finished and says why.
"""

import asyncio
import logging

from app import runs, trace
from app.agents import humanizer, writer
from app.report import render, validate
from app.runs import now
from app.storage import get_storage

log = logging.getLogger(__name__)
STATES = ("Writing", "Humanizing", "Validating", "Rendering")


def _state(run_id: str, state: str, detail: str = "", **report) -> None:
    with runs.lock(run_id):
        run = runs.get(run_id)
        runs.set_state(run, state, detail)
        if report:
            run.report = {**(run.report or {}), **report}
        runs.save(run)


def _checks_line(record: dict, ctx: validate.Context, draft) -> str:
    claims = sum(len(p.claims) for p in [*draft.priorities, *draft.paragraphs])
    line = f"Every number in this report was checked against the measurements ({claims} cited values)."
    if record["removed"]:
        line += " Removed because they did not pass the checks: " + "; ".join(record["removed"]) + "."
    return line


def _humanizer_line(info: dict) -> str:
    if info["status"] == "applied":
        n, r = len(info["edited"]), len(info["reverted"])
        return (f"Wording edited for readability in {n} places" + (f"; {r} edits were undone because they changed "
                "a number, a name or a hedge" if r else "") + ".")
    if info["status"] == "skipped":
        return "The wording edit was skipped."
    return "The wording edit failed, so this is the writer's draft."


async def build(run_id: str) -> str:
    """Write, check, edit and render the report. Returns the final state."""
    storage = get_storage()
    base = f"runs/{run_id}/report"
    _state(run_id, "Writing", status="writing", started_at=now())
    try:
        draft, ctx, record = await writer.write(run_id)
        storage.write(f"{base}/draft.json", writer.dump(draft).encode(), content_type="application/json")

        _state(run_id, "Humanizing")
        draft, info = await humanizer.edit(run_id, draft, ctx)

        _state(run_id, "Validating")
        event = trace.start(run_id, name="check_report", agent="writer", kind="helper")
        problems = validate.check(draft, ctx)
        if problems:  # the Humanizer's edits passed per block, so this only catches what the drop left
            draft, more = validate.drop(draft, problems, ctx)
            record["removed"].extend(more)
        trace.end(event, "passed" if not problems else f"removed: {'; '.join(record['removed'])}")
        storage.write(f"{base}/final.json", writer.dump(draft).encode(), content_type="application/json")
    except Exception as e:
        log.exception("run %s: report writing failed", run_id)
        reason = f"the report could not be written ({type(e).__name__}: {str(e)[:200]})"
        _finish(run_id, "Partial", reason, status="failed", error=reason)
        return "Partial"

    _state(run_id, "Rendering")
    run = runs.get(run_id)
    meta = {"checks": _checks_line(record, ctx, draft), "humanizer": _humanizer_line(info)}
    event = trace.start(run_id, name="render_report", agent="writer", kind="helper")
    web = render.html(run, draft, ctx, meta=meta)
    storage.write(f"{base}/report.html", web.encode(), content_type="text/html; charset=utf-8")
    pdf_error = None
    try:
        pdf = await asyncio.to_thread(render.pdf_bytes, render.html(run, draft, ctx, pdf=True, meta=meta))
        storage.write(f"{base}/report.pdf", pdf, content_type="application/pdf")
    except Exception as e:  # SPEC §10: keep the web report and say the PDF failed
        log.exception("run %s: PDF rendering failed", run_id)
        pdf_error = f"{type(e).__name__}: {str(e)[:200]}"
    trace.end(event, "HTML and PDF" if pdf_error is None else f"HTML only; PDF failed ({pdf_error})",
              status="ok" if pdf_error is None else "error")

    lost = [r for r in record["removed"] if r.startswith("priority ")]
    lost += [f"the {s.replace('_', ' ')} section" for s in validate.REQUIRED if not draft.section(s)]
    reasons = lost + (["the PDF could not be rendered"] if pdf_error else [])
    state = "Partial" if reasons else "Complete"
    _finish(run_id, state, "; ".join(reasons), status="ready", html_path=f"{base}/report.html",
            pdf_path=None if pdf_error else f"{base}/report.pdf", pdf_error=pdf_error,
            checks={"first": record["first"], "final": record["final"], "removed": record["removed"]},
            humanizer=info, at=now())
    return state


def _finish(run_id: str, state: str, detail: str, **report) -> None:
    with runs.lock(run_id):
        run = runs.get(run_id)
        run.report = {**(run.report or {}), **report}
        run.research["final_state"] = state
        runs.set_state(run, state, detail)
        runs.save(run)
    trace.flush(run_id)

