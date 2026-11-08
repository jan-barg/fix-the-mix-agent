"""Ask the README's sample queries on the deployed app, the way the browser does.

    uv run python scripts/sample_queries.py [--url URL] [--run RUN_ID]

Creates a demo run, sends sample 1 (the Lead records a brief), confirms the checked section maps,
starts the analysis (research, then the report), then asks samples 2 and 3 as follow-ups. Every reply
and its tool-call trace goes to demo/samples/agent.json. About $2 of model calls and 5–10 minutes.

The analysis is one request of several minutes, and a connection that long can be dropped on the way
while the run carries on. The script then follows the run's progress, as the browser does, and takes
the reply from the run's transcript and trace. --run continues a run this script started, from the
replies already in agent.json.
"""

import argparse
import json
import time

import httpx

from samples import OUT, QUERIES, ROOT, now, save

AUTO_START = "Sections confirmed — run the analysis."  # what static/app.js sends on "Confirm all"
STEPS = [("sample 1, brief", QUERIES[0]), ("sample 1, analysis", AUTO_START), ("sample 2", QUERIES[1]),
         ("sample 3", QUERIES[2])]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=json.loads((ROOT / "submission.json").read_text())["deploy_url"])
    parser.add_argument("--run", help="continue this run (its replies so far are in demo/samples/agent.json)")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    client = httpx.Client(base_url=url, timeout=3600)

    if args.run:
        record = json.loads((OUT / "agent.json").read_text())
        if record["run_id"] != args.run:
            raise SystemExit(f"demo/samples/agent.json holds run {record['run_id']}, not {args.run}.")
        run_id = args.run
    else:
        run_id = client.post("/api/demo").raise_for_status().json()["id"]
        record = {"url": url, "run_id": run_id, "started_at": now(), "queries": QUERIES, "exchanges": []}
    print(f"run {run_id}, watch it at {url}/#run={run_id}")

    def get(path: str, **params) -> dict:
        for attempt in range(4):  # reads are safe to repeat after a dropped connection
            try:
                return client.get(path, params=params).raise_for_status().json()
            except httpx.TransportError:
                if attempt == 3:
                    raise
                time.sleep(3)

    def get_run() -> dict:
        return get(f"/api/runs/{run_id}")

    def answered(message: str, run: dict) -> str | None:
        """The Lead's reply to this message if the run already has one (a reply this script lost)."""
        t = run["transcript"]
        for i, m in enumerate(t[:-1]):
            if m["role"] == "user" and m["text"] == message and t[i + 1]["role"] == "assistant":
                return t[i + 1]["text"]
        return None

    def recover(message: str, since: int) -> dict:
        while get(f"/api/runs/{run_id}/progress", since=since)["busy"]:
            time.sleep(5)
        text = answered(message, get_run())
        if text is None:
            raise SystemExit("The run stopped without replying; open it in the browser to see why.")
        events = get(f"/api/runs/{run_id}/progress", since=since)["events"]
        return {"response": text, "session_id": run_id, "tool_calls": events, "recovered": True}

    def chat(label: str, message: str) -> str:
        start = time.time()
        since = sum(len(e["tool_calls"]) for e in record["exchanges"])  # this message's first trace event
        if answered(message, get_run()):
            reply = recover(message, since)
        else:
            try:
                reply = client.post("/chat", json={"message": message, "session_id": run_id}).raise_for_status().json()
            except httpx.TransportError as err:
                print(f"\n({label}: {type(err).__name__}; following the run instead)")
                reply = recover(message, since)
        state = get_run()["state"]
        record["exchanges"].append({"label": label, "message": message, "seconds": round(time.time() - start),
                                    "state": state, **reply})
        save("agent.json", record)
        print(f"\n=== {label}: {state}, {round(time.time() - start)} s, {len(reply['tool_calls'])} tool calls\n"
              f"{reply['response']}")
        return state

    done = {e["label"] for e in record["exchanges"]}
    for label, message in STEPS:
        if label in done:
            continue
        if label == "sample 1, analysis" and get_run()["state"] == "ReviewingSections":
            client.post(f"/api/runs/{run_id}/sections/confirm").raise_for_status()
        if chat(label, message) == "NeedsClarification":
            print("\nThe Lead asked a question instead of recording a brief; stopping here.")
            return

    run = get_run()
    record.update(ended_at=now(), state=run["state"], usage=run["usage"], report=f"{url}/api/runs/{run_id}/report",
                  pdf=f"{url}/api/runs/{run_id}/report.pdf")
    print(f"\n{run['state']}, ${run['usage']['cost_usd']:.2f}; written to {save('agent.json', record)}")


if __name__ == "__main__":
    main()
