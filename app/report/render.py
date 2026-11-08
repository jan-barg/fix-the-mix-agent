"""The report as HTML and PDF (DESIGN.md §10): one Jinja2 template for both.

The web version links figures to the figure route; the PDF embeds them, and WeasyPrint renders the
same template with the print rules. Each figure is shown once, where the Writer first placed it,
with its title, a caption without timestamps and the exact passages under source details. Each number
the checks backed carries a marker that points to its measurement under Sources.
"""

import base64
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from app.agents import context
from app.agents.researchers import _close
from app.agents.schemas import Claim, ReportDraft
from app.agents.writer import plain_caption
from app.audio.features import clock
from app.evidence import Evidence
from app.report import validate
from app.runs import Run

TEMPLATES = Path(__file__).parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))

SECTION_TITLES = {
    "general": ("Overall tone and loudness", "Whole tracks at equal peak level"),
    "kick_low_end": ("Kick and low end", "Kick shape, low-end balance and ducking"),
    "stereo": ("Stereo and mono", "Width by frequency and what changes in mono"),
    "section_by_section": ("Section by section", "Matched sections and transitions"),
}
TOOL_NAMES = {
    "measure_loudness": "Loudness measurement", "measure_tonal_balance": "Tonal balance measurement",
    "measure_stereo": "Stereo measurement", "find_kick_candidates": "Kick search", "analyse_kick": "Kick analysis",
    "measure_low_end": "Low-end measurement", "render_spectrogram": "Spectrogram",
}
MODES = {"separate": "each reference compared on its own", "aspects": "different aspects from different references",
         "average": "one combined target"}
SENTENCE_END = re.compile(r"[.!?]\s")


def _passages(ev: Evidence, names: dict[str, str]) -> list[str]:
    return [f"{names.get(r['track_id'], r['track_id'])}, {r['label']}: {clock(r['start_s'])}–{clock(r['end_s'])}"
            for r in ev.source.get("ranges", [])]


def _figure(fig, ev: Evidence, names: dict[str, str], src: str) -> dict:
    return {"id": fig.id, "title": fig.title, "caption": plain_caption(fig.caption).removeprefix("Source: "),
            "src": src, "passages": _passages(ev, names), "tool": TOOL_NAMES.get(ev.tool, ev.tool),
            "status": ev.reliability["status"], "warnings": ev.reliability.get("warnings", [])}


def _claim_for(number: str, unit: str, claims: list[Claim]) -> Claim | None:
    n = float(number.replace("−", "-"))
    values = (n, n * 1000) if unit == "kHz" else (n,)
    same_unit = {unit, "Hz"} if unit == "kHz" else {unit}
    return next((c for c in claims if c.unit in same_unit and any(_close(v, c.value) for v in values)), None)


def _cite(text: str, claims: list[Claim], number: Callable[[str], int]) -> Markup:
    """The text with a marker after each cited number. Cited numbers in a row from one measurement, within
    a sentence, share the last one's marker. Numbers that name a band have no claim, so no marker."""
    hits = []
    for m in validate.WITH_UNIT.finditer(text):
        a, b, unit = m.groups()
        if claim := (b and _claim_for(b, unit, claims)) or _claim_for(a, unit, claims):
            hits.append((m, claim.evidence_id))
    out, pos = Markup(), 0
    for (m, ev_id), after in zip(hits, [*hits[1:], None]):
        if after and after[1] == ev_id and not SENTENCE_END.search(text, m.end(), after[0].start()):
            continue
        n = number(ev_id)
        out += escape(text[pos:m.end()]) + Markup(f'<a class="cite" href="#src-{n}" aria-label="Source {n}">[{n}]</a>')
        pos = m.end()
    return out + escape(text[pos:])


def view(run: Run, draft: ReportDraft, ctx: validate.Context, *, pdf: bool, meta: dict) -> dict:
    """Everything the template shows, with figures resolved and each figure placed once."""
    names = {t.id: t.display_name for t in run.tracks}
    by_id = {f.id: (f, ev) for ev in ctx.evidence.values() for f in ev.figures}
    shown: list[dict] = []
    seen: set[str] = set()
    numbers: dict[str, int] = {}  # evidence ID -> its number under Sources, in reading order

    def source(ev_id: str) -> int:
        return numbers.setdefault(ev_id, len(numbers) + 1)

    def cited(text: str, claims: list[Claim]) -> Markup:
        return _cite(text, [c for c in claims if validate._claim_problem(c, ctx) is None], source)

    def place(ids: list[str]) -> list[dict]:
        out = []
        for fid in ids:
            if fid in seen or fid not in by_id:
                continue
            seen.add(fid)
            fig, ev = by_id[fid]
            src = ("data:image/png;base64," + base64.b64encode(fig.png).decode()) if pdf else f"figures/{fid}"
            out.append({**_figure(fig, ev, names, src), "source": source(ev.id)})
        shown.extend(out)
        return out

    def paras(items) -> list[dict]:
        return [{"text": cited(p.text, p.claims), "figures": place(p.figure_ids)} for p in items]

    brief = run.brief or {}
    roles = {r["track_id"]: r for r in brief.get("reference_roles") or []}
    tracks = []
    for t in run.tracks:
        role = roles.get(t.id, {})
        tracks.append({"id": t.id, "name": t.display_name, "file": t.filename, "bpm": f"{t.grid.bpm:g}",
                       "duration": clock(t.source["duration_s"]), "aspects": ", ".join(role.get("aspects") or []),
                       "exclusions": ", ".join(role.get("exclusions") or []),
                       "gain": t.prep.get("applied_gain_db") if isinstance(t.prep, dict) else None})
    overview = paras(draft.section("overview"))
    priorities = [{"n": i, "title": p.title, "why": cited(p.why, p.claims), "rows": [
        (label, getattr(p, f) if f in validate.EXEMPT_FIELDS else cited(getattr(p, f), p.claims)) for label, f in (
            ("Measured", "observation"), ("Possible cause", "interpretation"), ("Try", "experiment"),
            ("Check", "verification")) if getattr(p, f).strip()], "figures": place(p.figure_ids)}
        for i, p in enumerate(draft.priorities, 1)]
    preserve = paras(draft.section("preserve"))
    sections = [{"key": k, "title": SECTION_TITLES[k][0], "kicker": SECTION_TITLES[k][1], "paras": paras(draft.section(k))}
                for k in SECTION_TITLES]
    method = paras(draft.section("method_notes"))
    limitations = paras(draft.section("limitations"))
    pairs = [{"user": p["user"].split("/", 1)[1],
              "refs": ", ".join(f"{names.get(context.track_of(v), v)} {v.split('/', 1)[1]}" for k, v in p.items()
                                if k != "user")} for p in context.matched_pairs(run)]
    user = run.track("user")
    sources = []
    for ev_id, n in numbers.items():  # a measurement behind a number may have no chart in the report
        ev = ctx.evidence[ev_id]
        figures = [f for f in shown if f["source"] == n]
        caption = figures[0]["caption"] if figures else plain_caption(
            ev.figures[0].caption if ev.figures else ev.source["description"]).removeprefix("Source: ")
        sources.append({"n": n, "figures": figures, "tool": TOOL_NAMES.get(ev.tool, ev.tool), "caption": caption,
                        "passages": _passages(ev, names)})
    return {
        "pdf": pdf,
        "title": "Mix report",
        "track_title": Path(user.filename).stem if user else "Your track",
        "date": datetime.now(timezone.utc).strftime("%-d %B %Y"),
        "tracks": tracks,
        "goals": brief.get("goals") or [],
        "keep_goals": brief.get("preserve") or [],
        "stage": {"mix": "Mix", "master": "Master"}.get(brief.get("production_stage"), "Not stated"),
        "mode": MODES.get(brief.get("comparison_mode"), MODES["separate"]),
        "combined": brief.get("combined_target"),
        "overview": overview, "priorities": priorities, "preserve": preserve, "sections": sections,
        "method": method, "limitations": limitations, "pairs": pairs, "sources": sources, **meta,
    }


def html(run: Run, draft: ReportDraft, ctx: validate.Context, *, pdf: bool = False, meta: dict | None = None) -> str:
    return env.get_template("report.html").render(**view(run, draft, ctx, pdf=pdf, meta=meta or {}))


def pdf_bytes(document: str) -> bytes:
    """WeasyPrint needs Pango; where it is missing (a dev machine) this raises OSError."""
    import weasyprint

    return weasyprint.HTML(string=document, base_url=str(TEMPLATES)).write_pdf()
