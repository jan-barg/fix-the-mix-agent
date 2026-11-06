"""Freeze a demo run's section maps into demo/corpus.json (DESIGN.md §12, Demo corpus).

Fix the demo sections in the editor (Accept each track), then:

    uv run python scripts/freeze_demo.py RUN_ID [--base https://fix-the-mix-….run.app]

Each demo track gets a "frozen" block: its file hash, file details, detected grid and the current
section map. POST /api/demo then builds runs from these without downloading or decoding the audio,
and every demo run starts from the same checked sections. Re-run after replacing demo audio.
"""

import argparse
import json
import urllib.request

from app.config import ROOT

DEPLOYED = "https://fix-the-mix-347988227457.us-east1.run.app"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--base", default=DEPLOYED, help="server the run lives on (default: the deployed app)")
    args = parser.parse_args()
    with urllib.request.urlopen(f"{args.base.rstrip('/')}/api/runs/{args.run_id}") as r:
        run = json.load(r)
    if not run["demo"]:
        raise SystemExit("That run was not created from the demo corpus.")

    path = ROOT / "demo" / "corpus.json"
    corpus = json.loads(path.read_text())
    by_path = {f"demo/audio/{e['file']}": e for e in corpus["tracks"]}
    for t in run["tracks"]:
        entry = by_path.get(t["storage_path"])
        if entry is None:
            raise SystemExit(f"{t['id']} ({t['storage_path']}) is not in demo/corpus.json.")
        m = run["section_maps"][t["id"]][-1]
        if not (m["accepted"] or m["confirmed"]):
            print(f"warning: {t['display_name']}'s map is not accepted; freezing it as it is")
        entry["frozen"] = {
            "sha256": t["source"]["sha256"],
            "source": t["source"],
            "prep": t["prep"],
            "grid": t["grid"],
            "map": {"bpm": m["bpm"], "downbeat_s": m["downbeat_s"], "grid_source": m["grid_source"],
                    "sections": [{k: s[k] for k in ("id", "label", "start_s", "end_s", "start_bar", "end_bar")}
                                 for s in m["sections"]]},
            "from_run": args.run_id,
        }
        print(f"{t['display_name']:<12} {entry['file']:<32} {m['bpm']:g} BPM ({m['grid_source']} grid): "
              + ", ".join(f"{s['label']} {s['start_s']:.1f}" for s in m["sections"]))
    path.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
