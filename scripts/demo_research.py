"""End-to-end research run on the demo corpus, in-process (DESIGN.md §12): brief, section
confirmation, research. Prints the replies, the trace timeline, the outcome and the cost.

    uv run python scripts/demo_research.py [--run RUN_ID]

Costs about $1 of model calls (Opus Lead, Sonnet researchers).
"""

import argparse
import json
import time

from fastapi.testclient import TestClient

from app import runs
from app.main import app


def timeline(events: list[dict]) -> None:
    for e in events:
        if e["kind"] == "helper":
            continue
        print(f"  {e['task_id']:>5} r{e['round'] or '-'} {e['kind']:<13} {e['agent']:<24} {e['name']:<22} "
              f"{e['started_at'][11:23]}–{(e['ended_at'] or '')[11:23]} {e['status']:<6} {e['result'][:90]!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", help="continue an existing run instead of creating a demo run")
    args = parser.parse_args()
    client = TestClient(app)
    with client:
        if args.run:
            run_id = args.run
        else:
            run_id = client.post("/api/demo").json()["id"]
            print("run", run_id)
            run = runs.get(run_id)
            message = run.comments.get("global") or "Please compare my track with the references."
            t = time.time()
            r = client.post("/chat", json={"message": message, "session_id": run_id}).json()
            print(f"\n=== brief ({time.time() - t:.0f}s, state {runs.get(run_id).state})\n{r['response']}")
            timeline(r["tool_calls"])
            print(json.dumps(runs.get(run_id).brief, indent=1))
            client.post(f"/api/runs/{run_id}/sections/confirm")
        t = time.time()
        r = client.post("/chat", json={"message": "Sections confirmed — run the analysis.", "session_id": run_id}).json()
        run = runs.get(run_id)
        print(f"\n=== research ({time.time() - t:.0f}s, state {run.state})\n{r['response']}")
        timeline(r["tool_calls"])
        finished = run.research.get("finished") or {}
        print("\nrounds used:", run.research["rounds_used"], " lead turns:", run.research.get("lead_turns"))
        for p in finished.get("priorities", []):
            print(f"- {p['title']}: {p['why']} {p['finding_refs']}")
        print("accepted:", len(finished.get("accepted", [])), " rejected:",
              [(x["finding_ref"], x["reason"][:80]) for x in finished.get("rejected", [])])
        print("usage:", run.usage)


if __name__ == "__main__":
    main()
