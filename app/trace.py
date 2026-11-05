"""Trace events (DESIGN.md §3.1, §8): every model call, tool call and helper run of a run, with timing.

An event is recorded when it starts (status "running"), so the progress panel can draw researchers
that are still working, and completed when it ends. Events live in memory and are written to
runs/<run_id>/trace.jsonl as a whole file (GCS objects cannot be appended to).
"""

import json
import threading
from contextvars import ContextVar
from typing import Any

from app.runs import now
from app.storage import get_storage

RESULT_CHARS = 500

_events: dict[str, list[dict]] = {}
_counters: dict[str, int] = {}
_guard = threading.Lock()

# The research round the current Lead turn is in, so helpers and researchers inherit it.
current_round: ContextVar[int | None] = ContextVar("current_round", default=None)


def _path(run_id: str) -> str:
    return f"runs/{run_id}/trace.jsonl"


def events(run_id: str) -> list[dict]:
    with _guard:
        if run_id not in _events:
            storage = get_storage()
            lines = storage.read(_path(run_id)).decode().splitlines() if storage.exists(_path(run_id)) else []
            _events[run_id] = [json.loads(line) for line in lines if line.strip()]
            for e in _events[run_id]:  # written by a process that has since stopped
                if e["status"] == "running":
                    e["status"], e["ended_at"] = "interrupted", e["started_at"]
            _counters[run_id] = max((int(e["task_id"][2:]) for e in _events[run_id] if e.get("task_id", "").startswith("t_")),
                                    default=0)
        return _events[run_id]


def start(run_id: str, *, name: str, agent: str, kind: str, args: Any = None,
          parent_task_id: str | None = None) -> dict:
    """Record the start of an event and return it; pass it to end() when the work finishes."""
    log = events(run_id)
    with _guard:
        _counters[run_id] += 1
        event = {
            "name": name, "agent": agent, "args": args if args is not None else {}, "result": "",
            "kind": kind, "task_id": f"t_{_counters[run_id]:02d}", "parent_task_id": parent_task_id,
            "round": current_round.get(), "started_at": now(), "ended_at": None, "status": "running",
        }
        log.append(event)
    return event


def end(event: dict, result: str, status: str = "ok", **extra: Any) -> dict:
    event["result"] = result if len(result) <= RESULT_CHARS else result[: RESULT_CHARS - 1] + "…"
    event["status"] = status
    event["ended_at"] = now()
    event.update(extra)
    return event


def since(run_id: str, index: int) -> list[dict]:
    return list(events(run_id)[index:])


def count(run_id: str) -> int:
    return len(events(run_id))


def flush(run_id: str) -> None:
    log = events(run_id)
    with _guard:
        data = "".join(json.dumps(e, default=str) + "\n" for e in log)
    get_storage().write(_path(run_id), data.encode(), content_type="application/x-ndjson")
