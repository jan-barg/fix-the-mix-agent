"""The Lead (DESIGN.md §7.1, §7.2; SPEC §2.2, §4): one /chat message in, the Lead's loop until it
answers the producer, asks a question or finishes research.

Phases follow the run: brief (until a brief is recorded and the sections are confirmed), research,
then follow-up. The tool set is the same in every phase so prompt caching holds; a call outside its
phase gets an error result. The Lead's conversation is append-only and lives in runs/<id>/lead.json.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app import runs, trace
from app.agents import context, llm, researchers, schemas, toolbox
from app.audio.features import clock
from app.config import settings
from app.report import pipeline
from app.runs import Run, now

log = logging.getLogger(__name__)

PROMPT = (Path(__file__).parent / "prompts" / "lead.md").read_text()
BRIEF_TURNS = 4  # Lead turns per message in the brief phase
FOLLOW_UP_TURNS = 6  # ... and per follow-up question
BUSY = "I'm still working on your previous message. The progress panel shows what is running."
INTERRUPTED = "Interrupted: the server restarted before this finished. Run it again if you still need it."


def _tool(name: str, description: str, model: type[BaseModel]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": schemas.inline_schema(model)}}


_MEASURE = " Runs the measurement (cached) and returns its evidence ID and summary. " + schemas.SCOPES_HELP
TOOLS = [
    _tool("get_run_overview", "Tracks, comments, confirmed section maps, matched section pairs, the brief, "
                              "research so far and the evidence already measured.", schemas.NoArgs),
    _tool("record_brief", "Record the analysis brief from the producer's comments (brief phase; also to update "
                          "it in the follow-up phase).", schemas.Brief),
    _tool("measure_loudness", "Integrated, short-term and peak loudness at equal sample peak, with differences "
                              "between tracks and from section to section." + _MEASURE, schemas.Scopes),
    _tool("measure_tonal_balance", "1/3-octave tonal balance and six broad band levels, with differences "
                                   "against the first scope." + _MEASURE, schemas.TonalArgs),
    _tool("measure_stereo", "Side-to-mid level, mono fold-down change and L/R correlation, in low, mid and high "
                            "regions." + _MEASURE, schemas.Scopes),
    _tool("find_kick_candidates", "The most exposed kicks in one track, with their onset times in seconds. "
                                  "Runs the measurement (cached) and returns its evidence ID and summary.",
          schemas.KickCandidateArgs),
    _tool("analyse_kick", "Attack, body, decay, pitch and level of one kick per track, from onset times found "
                          "by find_kick_candidates.", schemas.AnalyseKickArgs),
    _tool("measure_low_end", "How the sub, low, upper-bass and low-mid bands dip and recover around the kicks, "
                             "and the low-end spectrum." + _MEASURE, schemas.LowEndArgs),
    _tool("render_spectrogram", "A spectrogram figure of each scope on one shared scale (no numbers)." + _MEASURE,
          schemas.SpectrogramArgs),
    _tool("run_researcher", "Give one specialist one question. The harness measures the specialist's default "
                            "evidence on the scopes, adds the evidence you pass, and returns the researcher's "
                            "findings. All calls in one response run in parallel and count as one round.",
          schemas.RunResearcherArgs),
    _tool("finish_research", "Record which findings you accept and reject, the priorities, what to keep and the "
                             "limitations. Ends the research phase.", schemas.FinishResearchArgs),
]
MEASUREMENTS = set(toolbox.MEASUREMENTS)
PHASE_TOOLS = {
    "brief": {"get_run_overview", "record_brief"},
    "research": {"get_run_overview", *MEASUREMENTS, "run_researcher", "finish_research"},
    "follow_up": {"get_run_overview", "record_brief", *MEASUREMENTS, "run_researcher"},
}
PHASE_HINT = {
    "brief": "Measurements and researchers start once the brief is recorded and the producer has confirmed the sections.",
    "research": "The brief is fixed during research; note any change under limitations.",
    "follow_up": "Research is finished: answer the producer in your reply.",
}
FINAL_STATES = ("Writing", "Humanizing", "Validating", "Rendering", "Complete", "Partial")


class Finished(Exception):
    """finish_research succeeded: the loop stops and the summary is the reply."""

    def __init__(self, summary: str):
        self.summary = summary


# --- Phases and status ---


def resume_state(run: Run) -> str:
    """The state an Interrupted run was in."""
    last = run.state_history[-1] if run.state_history else {}
    return last.get("detail") or "ReviewingSections"


def phase(run: Run) -> str:
    state = resume_state(run) if run.state == "Interrupted" else run.state
    if run.research.get("finished") or state in FINAL_STATES or state == "FollowingUp":
        return "follow_up"
    if run.brief and run.sections_confirmed():
        return "research"
    return "brief"


def _section_line(s: context.NamedSection) -> str:
    return f"{s.name} {clock(s.start_s)}–{clock(s.end_s)} (bars {s.start_bar}–{s.end_bar - 1})"


def overview(run: Run) -> str:
    lines = ["Tracks:"]
    for t in run.tracks:
        lines.append(f"- {t.id}: {t.display_name}, \"{t.filename}\", {clock(t.source['duration_s'])}, "
                     f"{t.grid.bpm:g} BPM" + (f". Comment: {t.comment.strip()}" if t.comment.strip() else ""))
    if not run.tracks:
        lines.append("- none uploaded yet")
    lines.append(f"Global comment: {(run.comments or {}).get('global', '').strip() or '(none)'}")
    lines.append(f"Sections ({'confirmed' if run.sections_confirmed() else 'not confirmed yet'}):")
    for t in run.tracks:
        lines.append(f"- {t.id}: " + "; ".join(_section_line(s) for s in context.named_sections(run, t.id)))
    pairs = context.matched_pairs(run)
    if pairs:
        lines.append("Matched section pairs (your section: the references' sections of the same label and order):")
        lines.extend(f"- {p['user']}: " + (", ".join(v for k, v in p.items() if k != "user") or "no match")
                     for p in pairs)
    lines.append("Brief: " + (json.dumps(run.brief) if run.brief else "not recorded yet"))
    results = run.research.get("results", {})
    if results:
        lines.append("Researcher results so far: " + "; ".join(
            f"{tid} ({r['specialist']}, round {r['round']}): {r['question']}" for tid, r in results.items()))
    if run.evidence_index:
        lines.append("Evidence measured:")
        lines.extend(f"- {eid} ({e['tool']}): {e['description']}" for eid, e in run.evidence_index.items())
    return "\n".join(lines)


def _status(run: Run, p: str) -> str:
    r = run.research
    parts = [f"Phase: {p}.", f"Sections {'confirmed' if run.sections_confirmed() else 'not confirmed'}.",
             f"Brief {'recorded' if run.brief else 'not recorded'}."]
    if p != "brief":
        parts.append(f"Research rounds used: {r['rounds_used']} of {_round_limit(run, p)}.")
    if p == "research":
        parts.append(f"Lead turns used in research: {r.get('lead_turns', 0)} of {settings.max_lead_turns}.")
    if r.get("stale"):
        parts.append("The sections were edited after earlier research; those findings are archived and not available.")
    return "[Run status] " + " ".join(parts)


def _round_limit(run: Run, p: str) -> int:
    if p == "follow_up":
        return run.research.get("follow_up_round_limit", run.research["rounds_used"])
    return settings.max_research_rounds


SPECIALIST_STAGE = {"kick": "Comparing kicks", "low_end": "Comparing low-end",
                    "tonal_loudness": "Comparing tone and loudness", "stereo": "Comparing stereo"}
STATE_STAGE = {"Draft": "Waiting for tracks", "Preparing": "Preparing tracks", "ReviewingSections": "Review sections",
               "NeedsClarification": "Waiting for your answer", "FollowingUp": "Looking into your question",
               "Writing": "Writing the report", "Humanizing": "Editing the wording", "Validating": "Checking every number",
               "Rendering": "Rendering the report and PDF", "Partial": "Done", "Complete": "Done", "Failed": "Failed",
               "Interrupted": "Interrupted: send a message to continue"}


def stage(run: Run, events: list[dict]) -> str:
    """The plain-language progress line (SPEC §2.4)."""
    running = [e for e in events if e["status"] == "running"]
    specialists = [SPECIALIST_STAGE.get(e["name"].partition(":")[2], "Researching") for e in running
                   if e["name"].startswith("researcher:")]
    follow_up = any((e.get("round") or 0) > 1 for e in running)
    if specialists:
        return ("Checking a difference: " if follow_up else "") + ", ".join(dict.fromkeys(specialists))
    if any(e["kind"] == "helper" for e in running):
        return "Measuring"
    if run.state == "Researching":
        return "Weighing the findings" if run.research["rounds_used"] else "Planning the research"
    if run.state in ("ReviewingSections", "Draft", "Preparing") and any(e["agent"] == "lead" for e in running):
        return "Reading your notes"
    return STATE_STAGE.get(run.state, run.state)


# --- Conversation repair ---


def repair(messages: list[dict]) -> int:
    """Give every tool call of the last assistant message a result, so a conversation cut off by a
    restart is valid again. Returns how many results were added."""
    idx = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "assistant"), None)
    if idx is None:
        return 0
    answered = {m.get("tool_call_id") for m in messages[idx + 1:] if m["role"] == "tool"}
    missing = [c for c in messages[idx].get("tool_calls") or [] if c["id"] not in answered]
    messages.extend({"role": "tool", "tool_call_id": c["id"], "content": INTERRUPTED} for c in missing)
    return len(missing)


# --- Tools ---


class _Context:
    def __init__(self, run_id: str, p: str):
        self.run_id, self.phase = run_id, p


def _parse(model: type[BaseModel], raw: dict) -> Any:
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        err = e.errors()[0]
        raise toolbox.ToolError(f"Invalid arguments: {err['msg']} at {'.'.join(map(str, err['loc']))}") from None


async def _record_brief(ctx: _Context, raw: dict, task_id: str) -> str:
    brief = _parse(schemas.Brief, raw)
    with runs.lock(ctx.run_id):
        run = runs.get(ctx.run_id)
        ids = {t.id for t in run.tracks if t.role == "reference"}
        unknown = [r.track_id for r in brief.reference_roles if r.track_id not in ids]
        if unknown and run.tracks:
            raise toolbox.ToolError(f"No reference called {', '.join(unknown)}. References: {', '.join(sorted(ids))}.")
        run.brief = brief.model_dump()
        if ctx.phase == "brief" and run.state in ("Draft", "Preparing", "NeedsClarification"):
            runs.set_state(run, "ReviewingSections" if run.ready_for_sections() else ("Preparing" if run.tracks else "Draft"))
        runs.save(run)
    if ctx.phase == "follow_up":
        return "Brief updated."
    if run.sections_confirmed():
        return "Brief recorded. The sections are confirmed: start the research now."
    return ("Brief recorded. The producer has not confirmed the sections yet: end your turn with a short summary of "
            "the brief and ask them to check and confirm the sections.")


async def _measure(ctx: _Context, name: str, raw: dict, task_id: str) -> str:
    tb = toolbox.Toolbox(runs.get(ctx.run_id))
    ev = await asyncio.to_thread(tb.measure, name, raw, agent="lead", parent_task_id=task_id)
    return toolbox.lead_summary(ev)


async def _run_researcher(ctx: _Context, raw: dict, task_id: str) -> str:
    a = _parse(schemas.RunResearcherArgs, raw)
    task = researchers.Task(task_id=task_id, specialist=a.specialist, question=a.question, user_intent=a.user_intent,
                            scopes=a.scopes, evidence_ids=a.evidence_ids)
    record = await researchers.run_task(ctx.run_id, task)
    return researchers.for_lead(record)


def _finding(run: Run, ref: str) -> tuple[dict, dict] | None:
    """(researcher record, finding) for a reference like "t_04/o2"."""
    tid, _, fid = ref.strip().partition("/")
    record = run.research["results"].get(tid)
    if record is None:
        return None
    for kind in researchers.FINDING_KINDS:
        for f in record["result"][kind]:
            if f["id"] == fid:
                return record, {**f, "kind": kind[:-1]}
    return None


async def _finish_research(ctx: _Context, raw: dict, task_id: str) -> str:
    a = _parse(schemas.FinishResearchArgs, raw)
    run = runs.get(ctx.run_id)
    refs = [*a.accepted, *(r.finding_ref for r in a.rejected), *(f for p in a.priorities for f in p.finding_refs)]
    unknown = sorted({r for r in refs if _finding(run, r) is None})
    problems = []
    if unknown:
        problems.append(f"unknown finding references {', '.join(unknown)} (use the references shown in the researcher "
                        "results, like t_04/o2)")
    if len(a.priorities) > 3:
        problems.append(f"{len(a.priorities)} priorities; keep at most three")
    if not a.priorities and a.accepted:
        problems.append("no priorities: name at least one, citing accepted findings")
    outside = sorted({f for p in a.priorities for f in p.finding_refs} - set(a.accepted))
    if outside:
        problems.append(f"priorities cite findings that are not accepted: {', '.join(outside)}")
    warned = []
    for ref in a.accepted:
        found = _finding(run, ref)
        if found and found[1]["kind"] == "observation" and any(w.startswith(f"{found[1]['id']}:") for w in found[0]["warnings"]):
            warned.append(ref)
    if warned:
        problems.append(f"{', '.join(warned)} have check warnings: reject them or have them re-measured")
    if problems:
        raise toolbox.ToolError("Not finished: " + "; ".join(problems) + ".")
    accepted = {}
    for ref in a.accepted:
        record, finding = _finding(run, ref)
        accepted[ref.strip()] = {**finding, "specialist": record["specialist"], "task_id": record["task_id"]}
    with runs.lock(ctx.run_id):
        run = runs.get(ctx.run_id)
        run.research["finished"] = {**a.model_dump(), "accepted_findings": accepted, "at": now()}
        run.research["final_state"] = "Partial"  # until the report is rendered
        runs.set_state(run, "Writing")
        runs.save(run)
    raise Finished(a.summary)


HANDLERS = {
    "get_run_overview": lambda ctx, raw, tid: _overview(ctx),
    "record_brief": _record_brief,
    "run_researcher": _run_researcher,
    "finish_research": _finish_research,
}


async def _overview(ctx: _Context) -> str:
    return overview(runs.get(ctx.run_id))


async def _call(ctx: _Context, call: dict, allow_researcher: bool) -> tuple[str, str | None]:
    """Run one tool call; returns (result for the Lead, summary if it finished research)."""
    name = call["function"]["name"]
    try:
        raw = json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        raw = None
    event = trace.start(ctx.run_id, name=name, agent="lead", kind="llm_tool_call", args=raw if raw is not None else {})
    try:
        if raw is None or not isinstance(raw, dict):
            raise toolbox.ToolError("The arguments were not a JSON object.")
        if name not in HANDLERS and name not in MEASUREMENTS:
            raise toolbox.ToolError(f"There is no tool called {name}.")
        if name not in PHASE_TOOLS[ctx.phase]:
            raise toolbox.ToolError(f"{name} is not available in the {ctx.phase} phase. {PHASE_HINT[ctx.phase]}")
        if name == "run_researcher" and not allow_researcher:
            raise toolbox.ToolError("Round cap reached: no more researchers. "
                                    + ("Call finish_research." if ctx.phase == "research" else "Answer from what you have."))
        if name in MEASUREMENTS:
            out = await _measure(ctx, name, raw, event["task_id"])
        else:
            out = await HANDLERS[name](ctx, raw, event["task_id"])
        trace.end(event, out)
        return out, None
    except Finished as f:
        trace.end(event, "Research finished.")
        return "Research finished. The report is being written.", f.summary
    except toolbox.ToolError as e:
        trace.end(event, f"Error: {e}", status="error")
        return f"Error: {e}", None
    except Exception as e:  # a model or helper failure: the Lead sees it and can retry or move on
        log.exception("tool %s failed", name)
        trace.end(event, f"Error: {type(e).__name__}: {e}", status="error")
        return f"Error: {name} failed ({type(e).__name__}: {str(e)[:300]}).", None


async def _run_tools(run_id: str, p: str, tool_calls: list[dict]) -> tuple[list[dict], str | None]:
    """All tool calls of one Lead response, concurrently; results in call order."""
    ctx = _Context(run_id, p)
    researcher_calls = [c for c in tool_calls if c["function"]["name"] == "run_researcher"]
    allow, token = True, None
    if researcher_calls and p in ("research", "follow_up"):
        with runs.lock(run_id):
            run = runs.get(run_id)
            allow = run.research["rounds_used"] < _round_limit(run, p)
            if allow:
                run.research["rounds_used"] += 1
                runs.save(run)
            token = trace.current_round.set(run.research["rounds_used"] if allow else None)
    try:
        outcomes = await asyncio.gather(*(_call(ctx, c, allow) for c in tool_calls))
    finally:
        if token is not None:
            trace.current_round.reset(token)
    messages = [{"role": "tool", "tool_call_id": c["id"], "content": out} for c, (out, _) in zip(tool_calls, outcomes)]
    summary = next((s for _, s in outcomes if s is not None), None)
    return messages, summary


# --- The loop ---


def _set_state(run_id: str, state: str, detail: str = "", **research: Any) -> Run:
    with runs.lock(run_id):
        run = runs.get(run_id)
        runs.set_state(run, state, detail)
        run.research.update(research)
        runs.save(run)
        return run


def _tool_calls(message: dict) -> list[dict]:
    return [c if isinstance(c, dict) else c.model_dump() for c in message.get("tool_calls") or []]


async def chat(run_id: str, message: str) -> tuple[str, list[dict]]:
    """Handle one /chat message for a run. Returns the reply and the trace events it created."""
    if run_id in runs.active:  # checked and claimed with no await in between: one request per run
        return BUSY, []
    runs.active.add(run_id)
    start = trace.count(run_id)
    runs.add_message(run_id, "user", message)
    try:
        reply = await _turn(run_id, message)
    except Exception as e:
        log.exception("run %s: lead turn failed", run_id)
        run = runs.get(run_id)
        if run.state in runs.WORKING:
            _set_state(run_id, "Interrupted", run.state)
        reply = f"Something went wrong ({type(e).__name__}: {str(e)[:300]}). Send any message to try again."
    finally:
        runs.active.discard(run_id)
        trace.flush(run_id)
    runs.add_message(run_id, "assistant", reply)
    return reply, trace.since(run_id, start)


async def _report(run_id: str, summary: str) -> str:
    """Research is finished: write the report, then answer with the Lead's summary."""
    state = await pipeline.build(run_id)
    run = runs.get(run_id)
    if state == "Complete":
        return summary
    reason = run.state_history[-1].get("detail", "")
    return f"{summary}\n\nThe report is incomplete: {reason}." if reason else summary


async def _turn(run_id: str, message: str) -> str:
    run = runs.get(run_id)
    if run.state == "Interrupted":
        run = _set_state(run_id, resume_state(run))
    if run.state in pipeline.STATES:  # research finished, but the report was cut off
        return await _report(run_id, run.research["finished"]["summary"])
    messages = runs.load_lead(run_id)
    if not messages:
        messages.append({"role": "system", "content": [{"type": "text", "text": PROMPT,
                                                          "cache_control": {"type": "ephemeral"}}]})
    repair(messages)
    p = phase(run)
    if p == "follow_up":
        run = _set_state(run_id, "FollowingUp", follow_up_round_limit=run.research["rounds_used"] + 1)
    first = len(messages) == 1
    status = _status(run, p) + ("\n\n[Run overview]\n" + overview(run) if first else "")
    text = f"{message.strip()}\n\n{status}"
    if messages[-1]["role"] == "user":  # the previous message was never answered (a failed model call)
        messages[-1]["content"] += "\n\n" + text
    else:
        messages.append({"role": "user", "content": text})
    runs.save_lead(run_id, messages)

    turns, nudged = 0, False
    while True:
        run = runs.get(run_id)
        p = phase(run)
        if p == "research" and run.state != "Researching":
            run = _set_state(run_id, "Researching")
        research_turns = run.research.get("lead_turns", 0)
        if p == "research" and research_turns >= settings.max_lead_turns:
            return _stop_research(run_id, messages)
        cap = {"brief": BRIEF_TURNS, "follow_up": FOLLOW_UP_TURNS}.get(p)
        if cap is not None and turns >= cap:
            return _end_turn(run_id, p, "I couldn't finish that within my step limit. Ask again, more narrowly?")

        event = trace.start(run_id, name="lead", agent="lead", kind="model_call", args={"phase": p, "turn": turns + 1})
        try:
            c = await llm.complete("lead", messages, tools=TOOLS)
        except Exception as e:
            trace.end(event, f"{type(e).__name__}: {str(e)[:400]}", status="error")
            raise
        runs.add_usage(run_id, c.input_tokens, c.output_tokens, c.cost_usd)
        calls = _tool_calls(c.message)
        trace.end(event, c.text or ", ".join(x["function"]["name"] for x in calls), model=c.model,
                  input_tokens=c.input_tokens, output_tokens=c.output_tokens, cost_usd=c.cost_usd,
                  seconds=round(c.seconds, 2))
        messages.append(c.message)
        runs.save_lead(run_id, messages)
        turns += 1
        if p == "research":
            _set_state(run_id, "Researching", lead_turns=research_turns + 1)

        if not calls:
            if p == "research" and not nudged:
                nudged = True
                messages.append({"role": "user", "content": "[Harness] Research is still open. Continue with tools, "
                                                            "or call finish_research if you are done."})
                continue
            return _end_turn(run_id, p, c.text)

        results, summary = await _run_tools(run_id, p, calls)
        last_turn = p == "research" and research_turns + 1 == settings.max_lead_turns - 1
        if last_turn and summary is None:
            results[-1]["content"] += "\n\n[Harness] Your next turn is the last one in research: call finish_research."
        messages.extend(results)
        runs.save_lead(run_id, messages)
        trace.flush(run_id)
        if summary is not None:
            return await _report(run_id, summary)


def _end_turn(run_id: str, p: str, text: str) -> str:
    """The Lead answered without tools: a question or summary in the brief phase, an answer after research."""
    run = runs.get(run_id)
    if p == "brief" and not run.brief:
        _set_state(run_id, "NeedsClarification")
    elif p == "follow_up":
        _set_state(run_id, run.research.get("final_state", "Partial"))
    return text


def _stop_research(run_id: str, messages: list[dict]) -> str:
    """Turn cap reached without finish_research: end research with nothing accepted."""
    with runs.lock(run_id):
        run = runs.get(run_id)
        run.research["finished"] = {"accepted": [], "rejected": [], "priorities": [], "preserve": [],
                                    "limitations": ["Research stopped at the step limit before the Lead finished."],
                                    "summary": "", "accepted_findings": {}, "at": now(), "forced": True}
        run.research["final_state"] = "Partial"
        runs.set_state(run, "Partial", "research stopped at the step limit")
        runs.save(run)
    messages.append({"role": "user", "content": "[Harness] Research was stopped at the turn cap."})
    runs.save_lead(run_id, messages)
    return "I ran out of research steps before reaching firm conclusions. Ask me about a specific part and I'll dig in."


async def clear(run_id: str) -> None:
    """Start the Lead's conversation over: brief and research go, tracks and section maps stay."""
    if run_id in runs.active:
        raise RuntimeError("busy")
    with runs.lock(run_id):
        run = runs.get(run_id)
        run.brief = None
        run.transcript = []
        if not runs.archive_research(run):
            run.research = {**runs.fresh_research(), "stale": run.research.get("stale", [])}
        runs.set_state(run, "ReviewingSections" if run.ready_for_sections() else ("Preparing" if run.tracks else "Draft"))
        runs.save(run)
    runs.save_lead(run_id, [])
