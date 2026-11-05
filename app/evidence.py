"""Evidence records: what was measured, on which audio, how, and how reliable it is (SPEC §7.2).

Evidence IDs link claims to measurements internally and never appear in the report. Captions are
generated from the record's source, so prose cannot invent them.
"""

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from app.audio.features import Scope
from app.config import settings


@dataclass
class Figure:
    id: str
    title: str
    caption: str
    png: bytes = field(repr=False)
    path: str | None = None  # set once stored

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "caption": self.caption, "path": self.path}


@dataclass
class Evidence:
    id: str
    tool: str
    created_at: str
    source: dict[str, Any]
    method: dict[str, Any]
    results: dict[str, Any]  # {"scalars": {name: {"value", "unit"}}, "tables": {...}, "notes": [...]}
    figures: list[Figure]
    reliability: dict[str, Any]  # {"status": ok | qualified | unavailable, "warnings": [...]}
    repro: dict[str, Any]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["figures"] = [f.to_dict() for f in self.figures]
        return d

    def summary_text(self) -> str:
        """Compact text a researcher reads: scalars with units, notes, method and reliability. A scalar
        line reads "name = value unit"; names contain colons (times), so the separator is " = "."""
        lines = [f"Evidence {self.id} ({self.tool}): {self.source['description']}"]
        for name, s in self.results.get("scalars", {}).items():
            v = s["value"]
            shown = "unavailable" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))
            lines.append(f"  - {name} = {shown}{' ' + s['unit'] if s.get('unit') and v is not None else ''}")
        for note in self.results.get("notes", []):
            lines.append(f"  * {note}")
        method = "; ".join(f"{k}: {v}" for k, v in self.method.items() if k != "bands")
        lines.append(f"  Method: {method}")
        rel = self.reliability
        lines.append(f"  Reliability: {rel['status']}" + (f" ({'; '.join(rel['warnings'])})" if rel.get("warnings") else ""))
        if self.figures:
            lines.append("  Figures: " + "; ".join(f.title for f in self.figures))
        return "\n".join(lines)


def new_id(prefix: str = "ev") -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def cache_key(tool: str, scopes: list[Scope], params: dict[str, Any]) -> str:
    """Everything that affects a result: tool, file hashes, exact ranges, the beat grid (kick events
    and recovery-in-beats depend on it, and the user can replace it), parameters, config version."""
    def grid_id(s: Scope) -> list:
        g = s.track.features.grid
        return [round(g.bpm, 4), round(float(g.beats_s[0]), 4) if len(g.beats_s) else None, g.downbeat_phase]

    payload = {
        "tool": tool,
        "scopes": [[s.track.features.sha256, round(s.start_s, 3), round(s.end_s, 3), grid_id(s)] for s in scopes],
        "params": params,
        "config": settings.config_version,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def describe_scopes(scopes: list[Scope]) -> str:
    return " vs ".join(s.describe() for s in scopes)


def source(scopes: list[Scope], signal: str = "peak_normalised") -> dict[str, Any]:
    return {
        "description": describe_scopes(scopes),
        "track_ids": sorted({s.track.id for s in scopes}),
        "section_ids": [s.section_id for s in scopes if s.section_id],
        "ranges": [{"track_id": s.track.id, "label": s.label, "start_s": round(s.start_s, 3), "end_s": round(s.end_s, 3)}
                   for s in scopes],
        "signal": signal,
        "normalisation_gain_db": {s.track.id: s.track.audio.applied_gain_db for s in scopes},
    }


def scalar(value: float | int | str | None, unit: str = "") -> dict[str, Any]:
    """A reported number. NaN and ±inf (a silent passage's loudness, an undefined ratio) become None,
    "unavailable", so no sentinel value can be quoted as a measurement."""
    if isinstance(value, (bool, np.bool_)):
        value = bool(value)
    elif isinstance(value, (int, np.integer)):
        value = int(value)
    elif isinstance(value, (float, np.floating)):
        value = round(float(value), 3) if np.isfinite(value) else None
    return {"value": value, "unit": unit}


def make(tool: str, scopes: list[Scope], method: dict, results: dict, figures: list[Figure],
         warnings: list[str] | None = None, status: str | None = None, params: dict | None = None,
         signal: str = "peak_normalised") -> Evidence:
    warnings = warnings or []
    return Evidence(
        id=new_id(),
        tool=tool,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=source(scopes, signal),
        method=method,
        results=results,
        figures=figures,
        reliability={"status": status or ("qualified" if warnings else "ok"), "warnings": warnings},
        repro={"cache_key": cache_key(tool, scopes, params or {}), "config_version": settings.config_version,
               "file_sha256": {s.track.id: s.track.features.sha256 for s in scopes}},
    )


def figure(title: str, caption: str, png: bytes) -> Figure:
    return Figure(id=new_id("fig"), title=title, caption=caption, png=png)
