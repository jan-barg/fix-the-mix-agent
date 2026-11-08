"""The README's three sample queries, shared by sample_queries.py (the agent) and baselines.py (no tools).

Sample 1 is the demo's own request, which the app sends when the sections view opens. Samples 2 and 3
are follow-up questions asked in the same run after the report.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = json.loads((ROOT / "demo" / "corpus.json").read_text())
OUT = ROOT / "demo" / "samples"

QUERIES = [
    CORPUS["global_comment"],
    "How does my build-to-drop loudness jump compare with Reference 2, and where does mine differ?",
    "How does my stereo width in the drops compare with Reference 3, and what changes in mono?",
]


def setup() -> str:
    """What the producer gives the app besides the request: which track is which, and their comments.
    No measurements: the baseline gets only what a producer would type."""
    lines = []
    for t in sorted(CORPUS["tracks"], key=lambda t: t.get("slot", 0)):
        name = "My track" if t["role"] == "user" else f"Reference {t['slot']}"
        lines.append(f"{name}: {t['title']}. {t['comment']}")
    return "\n".join(lines)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(name: str, record: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n")
    return path
