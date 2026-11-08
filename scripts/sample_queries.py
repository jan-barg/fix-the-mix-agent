"""Ask the README's sample queries on the deployed app, the way the browser does.

    uv run python scripts/sample_queries.py [--url URL]

Creates a demo run, sends sample 1 (the Lead records a brief), confirms the checked section maps,
starts the analysis (research, then the report), then asks samples 2 and 3 as follow-ups. Every reply
and its tool-call trace goes to demo/samples/agent.json. About $2 of model calls and 5–10 minutes.
"""

import argparse
import json
import time

import httpx

from samples import QUERIES, ROOT, now, save

AUTO_START = "Sections confirmed — run the analysis."  # what static/app.js sends on "Confirm all"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=json.loads((ROOT / "submission.json").read_text())["deploy_url"])
    url = parser.parse_args().url.rstrip("/")
    client = httpx.Client(base_url=url, timeout=3600)  # the analysis is one request of several minutes

    run_id = client.post("/api/demo").raise_for_status().json()["id"]
    print(f"run {run_id}, watch it at {url}/#run={run_id}")
    record = {"url": url, "run_id": run_id, "started_at": now(), "queries": QUERIES, "exchanges": []}

    def chat(label: str, message: str) -> str:
        start = time.time()
        reply = client.post("/chat", json={"message": message, "session_id": run_id}).raise_for_status().json()
        state = client.get(f"/api/runs/{run_id}").raise_for_status().json()["state"]
        record["exchanges"].append({"label": label, "message": message, "seconds": round(time.time() - start),
                                    "state": state, **reply})
        save("agent.json", record)
        print(f"\n=== {label}: {state}, {round(time.time() - start)} s, {len(reply['tool_calls'])} tool calls\n"
              f"{reply['response']}")
        return state

    if chat("sample 1, brief", QUERIES[0]) == "NeedsClarification":
        print("\nThe Lead asked a question instead of recording a brief; stopping here.")
        return
    client.post(f"/api/runs/{run_id}/sections/confirm").raise_for_status()
    chat("sample 1, analysis", AUTO_START)
    for n, question in enumerate(QUERIES[1:], 2):
        chat(f"sample {n}", question)

    run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
    record.update(ended_at=now(), state=run["state"], usage=run["usage"], report=f"{url}/api/runs/{run_id}/report",
                  pdf=f"{url}/api/runs/{run_id}/report.pdf")
    print(f"\n{run['state']}, ${run['usage']['cost_usd']:.2f}; written to {save('agent.json', record)}")


if __name__ == "__main__":
    main()
