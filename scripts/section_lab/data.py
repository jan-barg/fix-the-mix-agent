"""The answer key (demo/section_truth.json: 8 tracks the producer sectioned by hand) with each track's analysis and
per-bar features, cached in .data/section_lab. Audio: the demo tracks from demo/audio, the rest from SECTION_AUDIO."""
import json
import os
import pickle
from pathlib import Path

from app.audio import decode, features
from app.audio.features import TrackData
from app.config import settings

REPO = Path(__file__).resolve().parents[2]
CACHE = REPO / ".data" / "section_lab"


def specs() -> list[dict]:
    return json.loads((REPO / "demo/section_truth.json").read_text())["tracks"]


def _path(spec: dict) -> Path:
    p = REPO / spec["file"]
    if p.exists():
        return p
    folder = os.environ.get("SECTION_AUDIO")
    if not folder or not (Path(folder) / spec["file"]).exists():
        raise SystemExit(f"Put {spec['file']} in the folder named by SECTION_AUDIO.")
    return Path(folder) / spec["file"]


def load() -> list[dict]:
    """Each spec with "track" (TrackData) and "F" (feats.bar_features), analysed once and then cached."""
    import feats  # noqa: PLC0415 (a sibling module: run these scripts from this folder's parent with PYTHONPATH set)

    CACHE.mkdir(parents=True, exist_ok=True)
    out = []
    for s in specs():
        cache = CACHE / f"{s['name']}.pkl"
        if cache.exists():
            td, F = pickle.loads(cache.read_bytes())
        else:
            path = _path(s)
            audio = decode.prepare(path, settings.analysis_sample_rate, settings.min_duration_s, settings.max_duration_s)
            td = TrackData("user", s["name"], "user", audio, features.compute(audio, features.file_sha256(path)))
            F = feats.bar_features(td)
            cache.write_bytes(pickle.dumps((td, F)))
        out.append({**s, "track": td, "F": F, "dur": td.audio.duration_s, "bar": 240 / s["bpm"]})
    return out
