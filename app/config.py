"""Settings from the environment: .env locally, Cloud Run env vars and secrets in production."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Gemini 3.5 models on Vertex are served from the global location (as in the starter).
os.environ.setdefault("VERTEXAI_LOCATION", "global")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "anthropic/claude-opus-5-5"
# Researchers run four at a time, several rounds per run: Sonnet costs half of Opus per token.
RESEARCHER_MODEL = "anthropic/claude-sonnet-5-5"


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


@dataclass(frozen=True)
class RoleConfig:
    """Which LiteLLM model a role uses and how hard it thinks."""

    model: str
    effort: str | None


@dataclass(frozen=True)
class Settings:
    # Storage: "local" keeps everything under data_dir; "gcs" uses the bucket.
    storage: str = _env("STORAGE", "local")
    bucket: str = _env("BUCKET", "")
    data_dir: Path = Path(_env("DATA_DIR", str(ROOT / ".data")))

    # Upload limits. Cloud Run rejects HTTP/1 request bodies over 32 MiB.
    max_upload_bytes: int = 30 * 1024 * 1024
    min_duration_s: float = 20.0
    max_duration_s: float = 600.0

    # Every track is analysed at this rate; the original rate is recorded.
    analysis_sample_rate: int = 44100

    # Research loop caps (SPEC §4.2).
    max_research_rounds: int = 3
    max_lead_turns: int = 12

    # Bump when any analysis setting changes; it is part of every cache key.
    config_version: str = "2026-11-05.7"


settings = Settings()

ROLES: dict[str, RoleConfig] = {
    "lead": RoleConfig(_env("LEAD_MODEL", DEFAULT_MODEL), "high"),
    "researcher": RoleConfig(_env("RESEARCHER_MODEL", RESEARCHER_MODEL), "medium"),
    "writer": RoleConfig(_env("WRITER_MODEL", DEFAULT_MODEL), "high"),
    "humanizer": RoleConfig(_env("HUMANIZER_MODEL", DEFAULT_MODEL), "medium"),
    # README baselines: same model as the Lead, no tools, no system prompt, default effort.
    "baseline": RoleConfig(_env("BASELINE_MODEL", _env("LEAD_MODEL", DEFAULT_MODEL)), None),
}
