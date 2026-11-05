"""One storage interface for run files: a local directory in development, a GCS bucket on Cloud Run.

Paths are relative POSIX strings such as "runs/<run_id>/run.json".
"""

import json
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from app.config import settings


class Storage(Protocol):
    def write(self, path: str, data: bytes, content_type: str | None = None) -> None: ...
    def read(self, path: str) -> bytes: ...
    def exists(self, path: str) -> bool: ...
    def delete(self, path: str) -> None: ...
    def list(self, prefix: str) -> list[str]: ...


def clean_path(path: str) -> str:
    """Reject absolute paths and '..' so a stored name can never escape its root."""
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"Invalid storage path: {path!r}")
    return p.as_posix()


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def _full(self, path: str) -> Path:
        return self.root / clean_path(path)

    def write(self, path: str, data: bytes, content_type: str | None = None) -> None:
        full = self._full(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_name(full.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(full)  # atomic, so a reader never sees half a file

    def read(self, path: str) -> bytes:
        return self._full(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._full(path).is_file()

    def delete(self, path: str) -> None:
        self._full(path).unlink(missing_ok=True)

    def list(self, prefix: str) -> list[str]:
        base = self._full(prefix)
        if not base.exists():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())


class GCSStorage:
    def __init__(self, bucket_name: str):
        from google.cloud import storage  # imported here so local development needs no GCP setup

        self.bucket = storage.Client().bucket(bucket_name)

    def write(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.bucket.blob(clean_path(path)).upload_from_string(data, content_type=content_type)

    def read(self, path: str) -> bytes:
        from google.api_core.exceptions import NotFound

        try:
            return self.bucket.blob(clean_path(path)).download_as_bytes()
        except NotFound:
            raise FileNotFoundError(path) from None  # same contract as LocalStorage

    def exists(self, path: str) -> bool:
        return self.bucket.blob(clean_path(path)).exists()

    def delete(self, path: str) -> None:
        blob = self.bucket.blob(clean_path(path))
        if blob.exists():
            blob.delete()

    def list(self, prefix: str) -> list[str]:
        return sorted(b.name for b in self.bucket.list_blobs(prefix=clean_path(prefix) + "/"))


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    if settings.storage == "gcs":
        if not settings.bucket:
            raise RuntimeError("STORAGE=gcs needs BUCKET to be set")
        return GCSStorage(settings.bucket)
    return LocalStorage(settings.data_dir)


# --- JSON helpers ---


def write_json(path: str, value: Any) -> None:
    data = json.dumps(value, indent=2, sort_keys=True, default=str).encode()
    get_storage().write(path, data, content_type="application/json")


def read_json(path: str) -> Any:
    return json.loads(get_storage().read(path))
