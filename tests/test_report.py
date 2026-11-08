"""The report without a model (DESIGN.md §7.4-§7.6, §10): the number and link checks, what is dropped
when a repair still fails, the Humanizer's protected content, and the whole pipeline with a scripted
Writer, from finished research to the HTML and PDF routes."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import api, runs
from app.agents import humanizer, llm, researchers, toolbox
from app.agents.schemas import (Claim, EditedBlock, EditedBlocks, FixedBlock, FixedBlocks, Para, ReportDraft,
                                ReportPriority, ResearcherResult)
from app.evidence import Evidence, Figure
from app.report import pipeline, render, validate
from tests.test_agents import make_run, store  # noqa: F401  (store is a fixture)

@pytest.fixture(autouse=True)
def report_store(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pipeline, "get_storage", lambda: store)
    return store


ATTACK = "Your track, drop 1 0:30–1:00 | attack-to-body ratio"
DIP = "Your track, drop 1 0:30–1:00 | upper bass 120-300 Hz | dip depth"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def make_evidence(eid: str = "ev_0000000a", figure_id: str = "fig_0000000a") -> Evidence:
    return Evidence(id=eid, tool="analyse_kick", created_at="", method={"body band": "30-120 Hz"},
                    source={"description": "Your track, drop 1 vs Reference 1, drop 1",
                            "ranges": [{"track_id": "user", "label": "drop 1", "start_s": 30.0, "end_s": 60.0}]},
                    results={"scalars": {ATTACK: {"value": -29.65, "unit": "dB"}, DIP: {"value": 17.12, "unit": "dB"}}},
                    figures=[Figure(id=figure_id, title="Kick body envelopes", caption="Source: kick envelope "
                                    "comparison of Your track, kick at 115.61 s vs Reference 1, kick at 236.99 s.", png=PNG)],
                    reliability={"status": "ok", "warnings": []}, repro={})


def ctx(**kw) -> validate.Context:
    ev = make_evidence()
    return validate.Context(evidence={ev.id: ev}, figures={"fig_0000000a"}, hz=kw.get("hz", {44.0}), facts={128.0})


def claim(value: float, name: str = ATTACK) -> Claim:
    return Claim(name=name, value=value, unit="dB", evidence_id="ev_0000000a")


def para(section: str, text: str, claims=(), figures=()) -> Para:
    return Para(section=section, text=text, claims=list(claims), figure_ids=list(figures))


def priority(observation: str = "Your attack-to-body ratio is -29.7 dB.", interpretation: str = "It could be the sample.",
             claims=None, figures=()) -> ReportPriority:
    return ReportPriority(title="More attack", why="The kick feels buried.", observation=observation,
                          interpretation=interpretation, experiment="Boost about 3 dB at 4 kHz on the click.",
                          verification="Re-measure drop 1 the same way.",
                          claims=[claim(-29.7)] if claims is None else claims, figure_ids=list(figures))


def draft(*paras: Para, priorities=None) -> ReportDraft:
    base = [para(s, f"{s} text.") for s in validate.REQUIRED]
    return ReportDraft(priorities=[priority()] if priorities is None else priorities, paragraphs=[*base, *paras])


# --- The checks ---


def test_every_number_with_a_unit_needs_a_claim_that_matches_the_evidence():
    c = ctx()
    check = lambda text, claims=(): validate.check(draft(para("general", text, claims)), c)  # noqa: E731
    assert check("Your ratio is -29.7 dB.", [claim(-29.7)]) == {}  # rounded to the quoted precision
    assert check("Your ratio is -30 dB.", [claim(-30)]) == {}
    # Rounded twice (-29.649 shown as -29.65, then said as -29.7), but not further off than that.
    assert researchers._close(-29.7, -29.649) and not researchers._close(-29.6, -29.66)
    wrong =check("Your ratio is -27 dB.", [claim(-27)])["paragraphs.7"][0]
    assert "-27 dB" in wrong and "the evidence says -29.65 dB" in wrong
    assert "-29.7 dB" in check("Your ratio is -29.7 dB.")["paragraphs.7"][0]  # no claim at all
    assert "17 dB" in check("It dips 17 dB.", [claim(17, name="no such scalar")])["paragraphs.7"][0]
    # Numbers that name things, frequencies the accepted findings state, and tempos need no claim.
    assert check("The 120-300 Hz band and the 30-120 Hz body band, at 128 BPM, near 44 Hz.") == {}
    assert "2.5 kHz" in check("A peak at 2.5 kHz.")["paragraphs.7"][0]
    # Both ends of a range are checked.
    assert "17 dB" in check("It dips between 3 and 17 dB.")["paragraphs.7"][0] or \
        "3 dB" in check("It dips 3-17 dB.")["paragraphs.7"][0]


def test_experiments_may_suggest_settings_but_every_other_priority_field_is_checked():
    c = ctx()
    assert validate.check(draft(), c) == {}  # "about 3 dB at 4 kHz" in the experiment needs no claim
    p = priority(interpretation="It could be the sample, which peaks 9 dB higher.")
    problems = validate.check(draft(priorities=[p]), c)
    assert list(problems) == ["priority1.interpretation"]


def test_internal_ids_missing_sections_and_unknown_figures_are_problems():
    d = draft(para("stereo", "As t_05/o1 shows, it is wide (see ev_1a2b3c4d).", figures=["fig_99999999"]))
    d.paragraphs = [p for p in d.paragraphs if p.section != "kick_low_end"]
    problems = validate.check(d, ctx())
    leak = next(v for k, v in problems.items() if k.startswith("paragraphs."))
    assert "internal identifier (t_05/o1)" in leak[0]
    assert "no kick low end paragraph" in problems["report"]
    assert any("fig_99999999" in p for p in problems["report"])


def test_what_still_fails_after_repair_is_dropped():
    good, bad_cause = priority(), priority(interpretation="It could be 9 dB of limiting.")
    bad_obs = priority(observation="Your ratio is -12 dB.", claims=[claim(-12)])
    d = draft(para("general", "It dips 40 dB."), para("general", "Fine.", figures=["fig_0000000a", "fig_99999999"]),
              priorities=[good, bad_cause, bad_obs])
    c = ctx()
    cleaned, removed = validate.drop(d, validate.check(d, c), c)
    assert [p.interpretation for p in cleaned.priorities] == ["It could be the sample.", ""]
    assert removed == ["the interpretation of priority 2", "priority 3 (More attack)", "a paragraph of general"]
    assert [p.text for p in cleaned.section("general")] == ["general text.", "Fine."]
    assert cleaned.section("general")[1].figure_ids == ["fig_0000000a"]  # the unknown figure is gone
    assert validate.check(cleaned, c) == {}


# --- The Humanizer ---


def completion(text: str) -> llm.Completion:
    return llm.Completion(message={"role": "assistant", "content": text}, text=text, tool_calls=[], finish_reason="stop",
                          model="fake", input_tokens=10, output_tokens=5, cost_usd=0.001, seconds=0.0)


def test_the_humanizer_keeps_an_edit_only_if_numbers_names_and_hedges_survive(store, monkeypatch):  # noqa: F811
    run = make_run()
    d = draft(para("section_by_section", "In drop 1 your bass could be ducked hard, which is consistent with a sidechain."),
              para("section_by_section", "Drop 2 of your track matches Reference 1."),
              para("general", "It is crucial to note that the mix is balanced."),
              para("general", "Your ratio is -29.7 dB.", [claim(-29.7)]))
    edits = {
        "paragraphs.7": "Your bass is ducked hard in drop 1 by a sidechain.",  # the hedge is gone
        "paragraphs.8": "The second drop matches Reference 1.",  # a section name is gone
        "paragraphs.9": "The mix is balanced.",  # a clean edit
        "paragraphs.10": "Your ratio is about -30 dB.",  # a number changed
        "priority1.why": "Right now the kick feels buried.",
    }

    async def fake(role, messages, schema):
        assert role == "humanizer" and schema is EditedBlocks
        sent = json.loads(messages[1]["content"])
        assert {b["id"] for b in sent} >= set(edits) and "claims" not in sent[0]  # prose only
        return EditedBlocks(blocks=[EditedBlock(id=k, text=v) for k, v in edits.items()]), completion("{}")

    monkeypatch.setattr(llm, "complete_json", fake)
    edited, info = asyncio.run(humanizer.edit(run.id, d, ctx()))
    assert sorted(info["edited"]) == ["paragraphs.9", "priority1.why"]
    assert info["reverted"]["paragraphs.7"] == ["hedging"]
    assert info["reverted"]["paragraphs.8"] == ["numbers", "track names", "section names"]
    assert info["reverted"]["paragraphs.10"][0] == "numbers"
    assert edited.paragraphs[8].text == "The mix is balanced."
    assert edited.paragraphs[6].text == d.paragraphs[6].text and edited.priorities[0].why == "Right now the kick feels buried."


def test_the_humanizer_can_be_turned_off(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(humanizer, "settings", type("S", (), {"humanizer": False})())
    d = draft()
    assert asyncio.run(humanizer.edit("unused", d, ctx())) == (d, {"status": "skipped", "reason": "turned off (HUMANIZER=off)"})


# --- From finished research to the report ---


def finished_run():
    run = make_run()
    ev = make_evidence()
    toolbox._store(run, ev, "key")
    obs = {"text": "Your kick has less attack.", "evidence_ids": [ev.id],
           "values": [{"name": ATTACK, "value": -29.65, "unit": "dB", "evidence_id": ev.id}]}
    result = ResearcherResult(question="Why is the kick buried?", scope="drop 1", observations=[obs], comparisons=[],
                              interpretations=[], experiments=[{"action": "Add a click.", "how_to_verify": "Re-measure.",
                                                                "supports_goal": "punch"}],
                              preserve=[], limitations=[], confidence="Good.", follow_up_requests=[])
    with runs.lock(run.id):
        run = runs.get(run.id)
        run.research["results"]["t_02"] = {
            "task_id": "t_02", "specialist": "kick", "question": "Why is the kick buried?", "user_intent": "punch",
            "scopes": [], "round": 1, "evidence": [{"id": ev.id, "tool": ev.tool, "description": "d"}],
            "result": researchers.with_ids(result), "warnings": [], "notes": []}
        accepted = {"t_02/o1": {**obs, "id": "o1", "kind": "observation", "specialist": "kick", "task_id": "t_02"}}
        run.research["finished"] = {"accepted": ["t_02/o1"], "rejected": [], "preserve": ["the tone"], "limitations": [],
                                    "priorities": [{"title": "More attack", "why": "punch", "finding_refs": ["t_02/o1"]}],
                                    "summary": "Add attack.", "accepted_findings": accepted}
        runs.set_state(run, "Writing")
        runs.save(run)
    return run


def scripted_writer(monkeypatch):
    calls = []
    first = draft(para("kick_low_end", "Your ratio is -29.7 dB, and the bass dips 20 dB.", [claim(-29.7)],
                       figures=["fig_0000000a"]),
                  priorities=[priority(figures=["fig_0000000a"])])

    async def fake(role, messages, schema):
        calls.append((role, schema.__name__))
        if schema is ReportDraft:
            return first, completion(first.model_dump_json())
        if schema is FixedBlocks:
            assert "paragraphs.7" in messages[-1]["content"] and "20 dB" in messages[-1]["content"]
            fix = FixedBlock(id="paragraphs.7", text="Your ratio is -29.7 dB, and the bass dips 17.1 dB.",
                             claims=[claim(-29.7), claim(17.1, name=DIP)])
            return FixedBlocks(blocks=[fix], new_paragraphs=[]), completion("{}")
        return EditedBlocks(blocks=[]), completion("{}")

    monkeypatch.setattr(llm, "complete_json", fake)
    return calls


def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_finished_research_becomes_a_checked_report_with_html_and_pdf(store, monkeypatch):  # noqa: F811
    run = finished_run()
    calls = scripted_writer(monkeypatch)
    monkeypatch.setattr(render, "pdf_bytes", lambda html: b"%PDF-1.7 fake")

    assert asyncio.run(pipeline.build(run.id)) == "Complete"
    assert calls == [("writer", "ReportDraft"), ("writer", "FixedBlocks"), ("humanizer", "EditedBlocks")]
    run = runs.get(run.id)
    assert run.state == "Complete" and run.research["final_state"] == "Complete"
    assert [h["state"] for h in run.state_history][-5:] == ["Writing", "Humanizing", "Validating", "Rendering", "Complete"]
    assert list(run.report["checks"]["first"]) == ["paragraphs.7"] and run.report["checks"]["final"] == {}

    c = client()
    page = c.get(f"/api/runs/{run.id}/report").text
    assert "the bass dips 17.1 dB" in page and "More attack" in page
    assert page.count('src="figures/fig_0000000a"') == 1  # shown once, where it was first placed
    assert "kick at 1:55" in page and "115.61" not in page
    assert "ev_0000000a" not in page and "t_02" not in page
    pdf = c.get(f"/api/runs/{run.id}/report.pdf")
    assert pdf.content == b"%PDF-1.7 fake" and "attachment" in pdf.headers["content-disposition"]


def test_a_pdf_failure_keeps_the_web_report_and_ends_partial(store, monkeypatch):  # noqa: F811
    run = finished_run()
    scripted_writer(monkeypatch)

    def no_pango(html):
        raise OSError("cannot load library 'libgobject-2.0-0'")

    monkeypatch.setattr(render, "pdf_bytes", no_pango)
    assert asyncio.run(pipeline.build(run.id)) == "Partial"
    run = runs.get(run.id)
    assert run.state_history[-1]["detail"] == "the PDF could not be rendered"
    c = client()
    assert c.get(f"/api/runs/{run.id}/report").status_code == 200
    missing = c.get(f"/api/runs/{run.id}/report.pdf")
    assert missing.status_code == 404 and "libgobject" in missing.json()["detail"]


def test_a_writer_failure_ends_partial_with_the_reason(store, monkeypatch):  # noqa: F811
    run = finished_run()

    async def broken(role, messages, schema):
        raise llm.ModelCallError("writer call ended with finish_reason='length'")

    monkeypatch.setattr(llm, "complete_json", broken)
    assert asyncio.run(pipeline.build(run.id)) == "Partial"
    run = runs.get(run.id)
    assert "the report could not be written" in run.state_history[-1]["detail"]
    assert client().get(f"/api/runs/{run.id}/report").status_code == 404


def test_a_passage_chart_ignores_sections_outside_the_passage():
    # A section label far outside the plotted passage stretched the tight-cropped chart past the 8000 px
    # models accept (35,203 px wide on a real run).
    import io

    import numpy as np
    from PIL import Image

    from app.audio import figures
    times = np.arange(100.0, 130.0, 0.1)
    series = [{"track_id": "user", "name": "Your track", "times_s": times,
               "short_term_lufs": np.full(times.size, -8.0), "bar_s": 240 / 128}]
    sections = {"user": [{"label": "intro", "start_s": 0.0, "end_s": 40.0},
                         {"label": "drop 1", "start_s": 95.0, "end_s": 160.0},
                         {"label": "outro", "start_s": 4000.0, "end_s": 4100.0}]}
    chart = figures.loudness_timeline(series, sections, {"user": (100.0, True)})
    width, height = Image.open(io.BytesIO(chart)).size
    assert width <= figures.WIDTH_IN * figures.DPI * 1.1 and height < 1000


# --- Citations ---


def test_cited_numbers_point_to_their_measurement_under_sources():
    first, second = make_evidence(), make_evidence("ev_0000000b", "fig_0000000b")
    second.figures = []  # a measurement with no chart in the report is still a source
    c = validate.Context(evidence={first.id: first, second.id: second}, figures={"fig_0000000a"}, facts={128.0})
    d = draft(para("general", "The attack is -29.7 dB against 17.1 dB in the 120-300 Hz band, kick & bass.",
                   [claim(-29.7), claim(17.1, DIP)], ["fig_0000000a"]),
              para("stereo", "The dip is 17.1 dB.", [Claim(name=DIP, value=17.1, unit="dB", evidence_id=second.id)]),
              para("section_by_section", "A wrong claim is -5.0 dB.", [claim(-5.0)]))
    page = render.html(make_run(), d, c)

    assert "Your attack-to-body ratio is -29.7 dB<a class=\"cite\" href=\"#src-1\"" in page
    # Two numbers from one measurement share a marker; a band has none; the prose stays escaped.
    assert "-29.7 dB against 17.1 dB<a class=\"cite\" href=\"#src-1\"" in page
    assert "300 Hz<a" not in page and "kick &amp; bass" in page
    assert "The dip is 17.1 dB<a class=\"cite\" href=\"#src-2\"" in page
    assert "A wrong claim is -5.0 dB.</p>" in page
    assert "<li id=\"src-1\"><span class=\"n\">[1]</span> <a href=\"#fig_0000000a\">" in page
    assert "<li id=\"src-2\"><span class=\"n\">[2]</span> <span class=\"title\">Kick analysis</span>" in page
