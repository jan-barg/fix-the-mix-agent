"""Redraw a finished run's charts and render its report again, after a change to the chart style
(app/audio/figures.py) or the report template (app/report/templates/report.html).

Charts are saved as images when a run is measured, and the report as HTML and PDF when it is written, so a
restyle reaches older runs only through this script. For each stored measurement it re-runs the tool call
recorded in the run's trace (same audio, sections and arguments, so the same numbers; no model calls) and
writes the new images over the old ones under the same ids, so the report's links still hold. A measurement
whose inputs have changed since (its cache key differs, for example after a section edit) is left as it was.
Then it renders the checked report (report/final.json) again as HTML and PDF.

Run it where the run lives, with the app's storage settings (STORAGE, BUCKET):

    uv run python scripts/rerender_report.py RUN_ID [--no-charts]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import runs, trace  # noqa: E402
from app.agents import writer  # noqa: E402
from app.agents.schemas import ReportDraft  # noqa: E402
from app.agents.toolbox import MEASUREMENTS, Toolbox, load  # noqa: E402
from app.report import render  # noqa: E402
from app.report.pipeline import _checks_line, _humanizer_line  # noqa: E402
from app.runs import now  # noqa: E402
from app.storage import get_storage  # noqa: E402


def redraw(run: runs.Run) -> None:
    calls: dict[str, dict] = {}
    for e in trace.events(run.id):
        if e["kind"] == "helper" and e["status"] == "ok" and e.get("evidence_id"):
            calls.setdefault(e["evidence_id"], e)
    box, storage = Toolbox(run), get_storage()
    for ev_id in run.evidence_index:
        old, call = load(run.id, ev_id), calls.get(ev_id)
        if not old.figures:
            continue
        if call is None:
            print(f"{ev_id}: no tool call for it in the trace; left as it was")
            continue
        compute, _ = box._plan(call["name"], MEASUREMENTS[call["name"]].model_validate(call["args"]))
        new = compute()
        if new.repro["cache_key"] != old.repro.get("cache_key"):
            print(f"{ev_id}: its inputs changed after it was measured; left as it was")
            continue
        if [f.title for f in new.figures] != [f.title for f in old.figures]:
            print(f"{ev_id}: the redraw made different charts; left as it was")
            continue
        for o, n in zip(old.figures, new.figures):
            storage.write(o.path, n.png, content_type="image/png")
        print(f"{ev_id} {call['name']}: {len(new.figures)} charts redrawn")


def rerender(run: runs.Run) -> None:
    storage, base = get_storage(), f"runs/{run.id}/report"
    draft = ReportDraft.model_validate_json(storage.read(f"{base}/final.json"))
    ctx = writer.report_context(run, writer.load_evidence(run))
    meta = {"checks": _checks_line(run.report["checks"], ctx, draft),
            "humanizer": _humanizer_line(run.report["humanizer"])}
    storage.write(f"{base}/report.html", render.html(run, draft, ctx, meta=meta).encode(),
                  content_type="text/html; charset=utf-8")
    update = {"at": now()}
    try:
        storage.write(f"{base}/report.pdf", render.pdf_bytes(render.html(run, draft, ctx, pdf=True, meta=meta)),
                      content_type="application/pdf")
        update |= {"pdf_path": f"{base}/report.pdf", "pdf_error": None}
        print("Rendered the HTML and the PDF.")
    except Exception as e:  # keep the earlier PDF, if there is one
        print(f"Rendered the HTML; the PDF failed ({type(e).__name__}: {str(e)[:200]}).")
    with runs.lock(run.id):
        live = runs.get(run.id)
        live.report = {**live.report, **update}
        runs.save(live)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--no-charts", action="store_true", help="only render the report again")
    args = parser.parse_args()
    run = runs.get(args.run_id)
    if (run.report or {}).get("status") != "ready":
        raise SystemExit("That run has no finished report.")
    if not args.no_charts:
        redraw(run)
    rerender(run)


if __name__ == "__main__":
    main()
