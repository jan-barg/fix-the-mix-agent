"""The agent layer without a model (DESIGN.md §7, §8, §12): scope references, the researcher values
check, and the Lead loop driven by a scripted fake model: phases, parallel researchers, the round
cap, finishing, resuming an interrupted run and refusing a second request."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import api, runs, storage, trace
from app.agents import context, lead, llm, researchers, toolbox
from app.agents.schemas import ResearcherResult
from app.evidence import Evidence
from app.storage import LocalStorage

SECTIONS = {
    "user": [("intro", 0, 30), ("drop", 30, 60), ("breakdown", 60, 90), ("drop", 90, 120)],
    "ref1": [("intro", 0, 20), ("drop", 20, 50), ("drop", 50, 80), ("outro", 80, 100)],
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = LocalStorage(tmp_path)
    for mod in (storage, runs, trace, toolbox, api):
        monkeypatch.setattr(mod, "get_storage", lambda: s)
    monkeypatch.setattr(runs, "_runs", {})
    monkeypatch.setattr(runs, "active", set())
    monkeypatch.setattr(trace, "_events", {})
    monkeypatch.setattr(trace, "_counters", {})
    return s


@pytest.fixture(autouse=True)
def no_report(monkeypatch):
    """The Lead's tests stop where the report starts; the report has its own tests (test_report.py)."""
    async def build(run_id):
        with runs.lock(run_id):
            run = runs.get(run_id)
            run.research["final_state"] = "Complete"
            runs.set_state(run, "Complete")
            runs.save(run)
        return "Complete"

    monkeypatch.setattr(lead.pipeline, "build", build)


def make_run(*, brief: bool = True, confirmed: bool = True) -> runs.Run:
    run = runs.Run(id="00000000-0000-4000-8000-000000000001", state="ReviewingSections")
    for tid, sections in SECTIONS.items():
        duration = sections[-1][2]
        run.tracks.append(runs.TrackRecord(
            id=tid, role="user" if tid == "user" else "reference", slot=None if tid == "user" else 1,
            display_name="Your track" if tid == "user" else "Reference 1", filename=f"{tid}.wav",
            storage_path=f"runs/x/{tid}.wav", source={"sha256": tid, "duration_s": float(duration)}, prep={},
            grid=runs.GridInfo(bpm=128.0, first_beat_s=0.0, downbeat_s=0.0, downbeat_confidence="high",
                               constant_tempo=True)))
        run.section_maps[tid] = [runs.SectionMap(
            version=1, origin="proposed", accepted=confirmed, confirmed=confirmed, bpm=128.0, downbeat_s=0.0,
            grid_source="detected",
            sections=[{"id": f"{tid}{i}", "label": label, "start_s": float(a), "end_s": float(b),
                       "start_bar": 1 + a * 128 // 240, "end_bar": 1 + b * 128 // 240}
                      for i, (label, a, b) in enumerate(sections)])]
    if brief:
        run.brief = {"goals": ["punchier drop"], "preserve": [], "reference_roles": [], "comparison_mode": "separate",
                     "focus_sections": ["drop"], "production_stage": "mix", "assumptions": []}
    runs.save(run)
    return run


def tracks() -> dict:
    return {tid: SimpleNamespace(id=tid, name=tid, audio=SimpleNamespace(duration_s=float(s[-1][2])))
            for tid, s in SECTIONS.items()}


# --- Scope references ---


def test_repeated_labels_get_ordinals_and_pair_by_order(store):
    run = make_run()
    assert [s.name for s in context.named_sections(run, "user")] == ["intro", "drop 1", "breakdown", "drop 2"]
    pairs = context.matched_pairs(run)
    assert {"user": "user/drop 2", "ref1": "ref1/drop 2"} in pairs
    assert {"user": "user/breakdown"} in pairs  # no breakdown in the reference: no partner, not a substitute
    assert context.default_focus(run) == ["user/drop 1", "ref1/drop 1"]


def test_scope_references_resolve_to_the_named_ranges(store):
    run, t = make_run(), tracks()
    drop2 = context.resolve(run, "ref1/drop 2", t)
    assert (drop2.start_s, drop2.end_s, drop2.section_id) == (50.0, 80.0, "ref12")
    assert context.resolve(run, "user", t).is_whole
    passage = context.resolve(run, "user@60.5-90", t)
    assert (passage.start_s, passage.end_s) == (60.5, 90.0)
    for bad, message in [("user/drop 3", "no section called"), ("ref2/drop 1", "no track"),
                         ("user@100-130", "inside the track"), ("user@10-10.5", "at least 1 s")]:
        with pytest.raises(context.ScopeError, match=message):
            context.resolve(run, bad, t)


# --- The values check ---


def evidence(scalars: dict) -> Evidence:
    return Evidence(id="ev_1", tool="measure_loudness", created_at="", method={"window": "3 s"},
                    source={"description": "Your track vs Reference 1"}, figures=[], repro={},
                    reliability={"status": "ok", "warnings": []},
                    results={"scalars": {k: {"value": v, "unit": u} for k, (v, u) in scalars.items()}})


def result(observations: list[dict], experiments: list[dict] | None = None,
           comparisons: list[dict] | None = None) -> ResearcherResult:
    return ResearcherResult.model_validate({
        "question": "q", "scope": "s", "observations": observations, "comparisons": comparisons or [], "interpretations": [],
        "experiments": experiments or [], "preserve": [], "limitations": [], "confidence": "c", "follow_up_requests": []})


def test_quoted_values_must_match_the_cited_scalar_after_rounding():
    ev = {"ev_1": evidence({"Your track | integrated loudness": (-8.764, "LUFS"),
                            "Your track minus Reference 1 | integrated loudness": (-2.351, "LU")})}

    def obs(text, name, value, evidence_id="ev_1"):
        return {"text": text, "evidence_ids": [evidence_id],
                "values": [{"name": name, "value": value, "unit": "LUFS", "evidence_id": evidence_id}]}

    ok = result([obs("Your track reads -8.8 LUFS.", "Your track | integrated loudness", -8.8),
                 obs("It is 2.35 LU quieter.", "Your track minus Reference 1 | integrated loudness", 2.35),  # sign dropped
                 obs("About -9 LUFS.", "Your track | integrated loudness", -9)])
    assert researchers.check(ok, ev) == []

    bad = result([obs("Your track reads -8.6 LUFS.", "Your track | integrated loudness", -8.6),
                  obs("x", "Your track | true peak", -1.0),
                  obs("y", "Your track | integrated loudness", -8.76, evidence_id="ev_9")])
    warnings = researchers.check(bad, ev)
    assert any(w.startswith("o1: quotes -8.6") for w in warnings)
    assert any(w.startswith("o2:") and "no scalar named" in w for w in warnings)
    assert any(w.startswith("o3:") and "not given" in w for w in warnings)


def test_numbers_in_prose_need_a_quoted_value():
    ev = {"ev_1": evidence({"Your track | integrated loudness": (-8.764, "LUFS")})}
    r = result([{"text": "Your track reads -8.8 LUFS over a 3 s window, 4 dB louder than needed.",
                 "evidence_ids": ["ev_1"], "values": [{"name": "Your track | integrated loudness", "value": -8.8,
                                                       "unit": "LUFS", "evidence_id": "ev_1"}]}],
               experiments=[{"action": "Cut 6 dB at 200 Hz.", "how_to_verify": "re-measure", "supports_goal": "g"}],
               comparisons=[{"target": "Reference 1", "text": "Reference 1 is 7 LU louder.", "evidence_ids": []}])
    warnings = researchers.check(r, ev)
    assert any(w.startswith("o1: states 4 dB") for w in warnings)
    assert any(w.startswith("c1: states 7 LU") for w in warnings)
    assert len(warnings) == 2  # "3 s" names the method's window; the experiment's settings are suggestions


# --- The Lead loop with a scripted model ---


def call(name: str, i: int, **args) -> dict:
    return {"id": f"call_{name}_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def completion(text: str = "", calls: list[dict] | None = None) -> llm.Completion:
    message = {"role": "assistant", "content": text or None, "tool_calls": calls or None}
    return llm.Completion(message=message, text=text, tool_calls=calls or [], finish_reason="tool_calls" if calls else "stop",
                          model="fake", input_tokens=10, output_tokens=5, cost_usd=0.001, seconds=0.0)


class Script:
    """A fake llm.complete that plays responses in order; a callable response sees the run."""

    def __init__(self, monkeypatch, run_id: str, *responses):
        self.responses, self.run_id, self.seen = list(responses), run_id, []
        monkeypatch.setattr(lead.llm, "complete", self)

    async def __call__(self, role, messages, *, tools=None, response_format=None):
        self.seen.append(json.loads(json.dumps(messages, default=str)))
        r = self.responses.pop(0)
        return r(runs.get(self.run_id)) if callable(r) else r


def fake_researchers(monkeypatch, delay: float = 0.05):
    spans = []

    async def run_task(run_id, task):
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(delay)
        spans.append((task.specialist, start, asyncio.get_running_loop().time(), trace.current_round.get()))
        obs = {"text": "measured", "evidence_ids": ["ev_1"], "values": []}
        r = result([obs], experiments=[{"action": "try", "how_to_verify": "check", "supports_goal": "g"}])
        record = {"task_id": task.task_id, "specialist": task.specialist, "question": task.question,
                  "user_intent": task.user_intent, "scopes": task.scopes, "round": trace.current_round.get(),
                  "evidence": [], "result": researchers.with_ids(r), "notes": [],
                  "warnings": ["o1: quotes 3 for 'x', but the evidence says 4 dB"] if task.specialist == "stereo" else []}
        with runs.lock(run_id):
            live = runs.get(run_id)
            live.research["results"][task.task_id] = record
            runs.save(live)
        return record

    monkeypatch.setattr(lead.researchers, "run_task", run_task)
    return spans


def researcher(i: int, specialist: str) -> dict:
    return call("run_researcher", i, specialist=specialist, question=f"{specialist}?", user_intent="punch")


def refs(run: runs.Run, specialist: str) -> list[str]:
    tid = next(t for t, r in run.research["results"].items() if r["specialist"] == specialist and r["round"] == 2)
    return [f"{tid}/o1", f"{tid}/x1"]


def finish(run: runs.Run) -> llm.Completion:
    accepted = refs(run, "low_end")
    return completion(calls=[call("finish_research", 9, accepted=accepted, rejected=[],
                                  priorities=[{"title": "Low end", "why": "punch", "finding_refs": accepted}],
                                  preserve=["the tone"], limitations=[], summary="Tighten the low end.")])


def test_a_research_run_fans_out_in_parallel_then_follows_up_and_finishes(store, monkeypatch):
    run = make_run()
    spans = fake_researchers(monkeypatch)
    script = Script(monkeypatch, run.id,
                    completion(calls=[researcher(1, "kick"), researcher(2, "low_end")]),
                    completion(calls=[researcher(3, "low_end")]),
                    finish)
    reply, events = asyncio.run(lead.chat(run.id, "Sections confirmed — run the analysis."))

    assert reply == "Tighten the low end."
    (_, k0, k1, r1), (_, l0, l1, _), (*_, r2) = sorted(spans, key=lambda s: s[1])
    assert k0 < l1 and l0 < k1, "the first round's researchers overlap"
    assert (r1, r2) == (1, 2)
    run = runs.get(run.id)
    assert run.state == "Complete" and run.research["rounds_used"] == 2
    assert set(run.research["finished"]["accepted_findings"]) == set(refs(run, "low_end"))
    tools = [e for e in events if e["kind"] == "llm_tool_call"]
    assert [e["round"] for e in tools] == [1, 1, 2, None]
    assert {"Researching", "Writing", "Complete"} <= {h["state"] for h in run.state_history}
    # The first message carries the overview; every message the status block.
    first = script.seen[0][1]["content"]
    assert "[Run status] Phase: research." in first and "[Run overview]" in first
    assert run.usage["calls"] == 3


def test_the_round_cap_turns_researcher_calls_into_errors(store, monkeypatch):
    run = make_run()
    run.research["rounds_used"] = 3
    runs.save(run)
    spans = fake_researchers(monkeypatch)
    script = Script(monkeypatch, run.id,
                    completion(calls=[researcher(1, "kick")]),
                    completion(calls=[call("finish_research", 2, accepted=[], rejected=[], priorities=[], preserve=[],
                                           limitations=["out of rounds"], summary="Done.")]))
    reply, _ = asyncio.run(lead.chat(run.id, "go"))
    assert reply == "Done." and spans == []
    assert script.seen[1][-1]["content"].startswith("Error: Round cap reached")


def test_characters_escaped_twice_in_arguments_reach_the_producer_as_characters(store, monkeypatch):
    run = make_run()
    Script(monkeypatch, run.id, completion(calls=[call("finish_research", 1, accepted=[], rejected=[], priorities=[],
                                                       preserve=[], limitations=[], summary="Sub at 28\\u201335 Hz.")]))
    reply, _ = asyncio.run(lead.chat(run.id, "go"))
    assert reply == "Sub at 28–35 Hz."
    assert lead._unescape(["\\ud83d", {"a": "\\u00e9"}]) == ["\\ud83d", {"a": "é"}]


def test_finish_research_refuses_unknown_and_unchecked_findings(store, monkeypatch):
    run = make_run()
    fake_researchers(monkeypatch)

    def bad_finish(run):
        stereo = next(t for t, r in run.research["results"].items() if r["specialist"] == "stereo")
        return completion(calls=[call("finish_research", 3, accepted=["t_99/o1", f"{stereo}/o1"], rejected=[],
                                      priorities=[{"title": "x", "why": "y", "finding_refs": [f"{stereo}/x1"]}],
                                      preserve=[], limitations=[], summary="s")])

    script = Script(monkeypatch, run.id, completion(calls=[researcher(1, "stereo")]), bad_finish,
                    completion("Stopping here."))
    asyncio.run(lead.chat(run.id, "go"))
    error = script.seen[2][-1]["content"]
    assert error.startswith("Error: Not finished")
    assert "unknown finding references t_99/o1" in error and "check warnings" in error and "not accepted" in error
    assert runs.get(run.id).research["finished"] is None


def test_the_brief_phase_rejects_measurements_and_waits_for_an_answer(store, monkeypatch):
    run = make_run(brief=False, confirmed=False)
    script = Script(monkeypatch, run.id,
                    completion(calls=[call("measure_loudness", 1, scopes=["user"])]),
                    completion("Is this a mix or a master?"))
    reply, events = asyncio.run(lead.chat(run.id, "Make it as loud as the reference."))
    assert reply == "Is this a mix or a master?"
    assert "not available in the brief phase" in script.seen[1][-1]["content"]
    assert events[1]["status"] == "error"
    assert runs.get(run.id).state == "NeedsClarification"

    brief = {"goals": ["loud"], "preserve": [], "reference_roles": [{"track_id": "ref1", "aspects": ["loudness"],
                                                                     "exclusions": []}],
             "comparison_mode": "separate", "focus_sections": [], "production_stage": "master", "assumptions": []}
    Script(monkeypatch, run.id, completion(calls=[call("record_brief", 2, **brief)]), completion("Check the sections."))
    reply, _ = asyncio.run(lead.chat(run.id, "A master."))
    run = runs.get(run.id)
    assert reply == "Check the sections." and run.brief["production_stage"] == "master"
    assert run.state == "ReviewingSections"


def test_a_second_message_while_working_is_refused(store, monkeypatch):
    run = make_run()
    runs.active.add(run.id)
    assert asyncio.run(lead.chat(run.id, "hello?")) == (lead.BUSY, [])
    with pytest.raises(Exception) as e:  # and the sections cannot change under it
        api._not_running(runs.get(run.id))
    assert e.value.status_code == 409


def test_an_interrupted_run_resumes_with_its_dangling_calls_answered(store, monkeypatch):
    run = make_run()
    runs.set_state(run, "Researching")
    runs.save(run)
    dangling = completion(calls=[researcher(1, "kick"), researcher(2, "low_end")]).message
    runs.save_lead(run.id, [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}, dangling,
                            {"role": "tool", "tool_call_id": "call_run_researcher_1", "content": "done"}])
    trace.flush(run.id)
    monkeypatch.setattr(runs, "_runs", {})  # a fresh process

    assert runs.get(run.id).state == "Interrupted"
    script = Script(monkeypatch, run.id, completion(calls=[call("finish_research", 3, accepted=[], rejected=[],
                                                                priorities=[], preserve=[], limitations=[],
                                                                summary="Resumed.")]))
    reply, _ = asyncio.run(lead.chat(run.id, "continue"))
    sent = script.seen[0]
    assert reply == "Resumed."
    assert [m["role"] for m in sent[-3:]] == ["tool", "tool", "user"]
    assert sent[-2] == {"role": "tool", "tool_call_id": "call_run_researcher_2", "content": lead.INTERRUPTED}
    assert runs.get(run.id).state == "Complete"


def test_editing_sections_after_research_archives_the_findings(store):
    run = make_run()
    run.research["results"]["t_02"] = {"specialist": "kick"}
    run.research["finished"] = {"summary": "x"}
    runs.set_state(run, "Partial")
    runs.save(run)
    body = {"sections": [{"label": "intro", "start_s": 0, "end_s": 40}, {"label": "drop", "start_s": 40, "end_s": 120}],
            "bpm": 128, "downbeat_s": 0, "grid_source": "detected"}
    app = FastAPI()
    app.include_router(api.router)
    r = TestClient(app).put(f"/api/runs/{run.id}/tracks/user/sections", json=body)
    assert r.status_code == 200
    run = runs.get(run.id)
    assert run.state == "ReviewingSections" and not run.sections_confirmed()
    assert run.research["results"] == {} and run.research["stale"][0]["results"] == {"t_02": {"specialist": "kick"}}
    assert lead.phase(run) == "brief"  # research starts again once the new map is confirmed
