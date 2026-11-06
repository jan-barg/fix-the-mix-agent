"""Companion endpoints for the web UI (DESIGN.md §3.3): runs, uploads, audio, peaks and section maps."""

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app import runs, trace
from app.agents import lead
from app.audio import features, peaks, sections
from app.audio.decode import AudioError
from app.audio.features import TrackData
from app.config import ROOT, settings
from app.runs import Run, SectionMap, TrackRecord
from app.storage import get_storage

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav"}
TOO_LARGE = (f"That file is larger than {settings.max_upload_bytes // (1024 * 1024)} MB. Please export an MP3 "
             "(320 kbps works well) and upload that instead.")


def _run(run_id: str) -> Run:
    try:
        return runs.get(run_id)
    except runs.RunNotFound:
        raise HTTPException(404, "This run does not exist.") from None


def _track(run: Run, track_id: str) -> TrackRecord:
    t = run.track(track_id)
    if t is None:
        raise HTTPException(404, "This track has not been uploaded.")
    return t


def _payload(run: Run) -> dict:
    return run.model_dump(mode="json")


def _not_running(run: Run) -> None:
    """Tracks and sections cannot change under a running analysis (A12)."""
    if run.id in runs.active or run.state in runs.WORKING:
        raise HTTPException(409, "The analysis is running. Wait for it to finish before changing tracks or sections.")


def _track_id(role: str, slot: int | None) -> tuple[str, str]:
    if role == "user":
        return "user", "Your track"
    if role == "reference" and slot in (1, 2, 3):
        return f"ref{slot}", f"Reference {slot}"
    raise HTTPException(400, "A track is either your track or reference 1, 2 or 3.")


# --- Runs ---


@router.post("/runs")
def create_run():
    return {"run_id": runs.create().id}


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    return _payload(_run(run_id))


class CommentsIn(BaseModel):
    global_: str = Field("", alias="global", max_length=4000)
    tracks: dict[str, str] = Field(default_factory=dict)


@router.put("/runs/{run_id}/comments")
def put_comments(run_id: str, body: CommentsIn):
    _run(run_id)
    with runs.lock(run_id):
        run = runs.get(run_id)
        run.comments["global"] = body.global_
        for tid, text in body.tracks.items():
            if (t := run.track(tid)) is not None:
                t.comment = text[:2000]
        runs.save(run)
    return _payload(run)


# --- Tracks ---


def _analyse(tid: str, display: str, role: str, data: bytes, suffix: str) -> tuple[TrackData, dict]:
    """Decode, normalise, base analysis and peaks for one file; a file already in memory is reused."""
    sha = hashlib.sha256(data).hexdigest()
    storage = get_storage()
    with runs.audio_cache.key_lock(sha):
        cached = runs.audio_cache.lookup(sha)
        if cached is None:
            with runs.analysis_slots:
                audio = runs.prepare_bytes(data, suffix)
                if audio.silent:
                    raise AudioError("This file is silent, so there is nothing to measure.")
                feats = features.get_or_compute(audio, sha, storage)
                peaks.get_or_compute(audio, sha, storage)
            runs.audio_cache.put(sha, audio, feats)
        else:
            audio, feats = cached
    td = TrackData(tid, display, role, audio, feats)
    p = audio.probe
    source = {"sha256": sha, "bytes": len(data), "format": p.format_name, "codec": p.codec, "sample_rate": p.sample_rate,
              "channels": p.channels, "duration_s": audio.duration_s, "decode_warnings": audio.decode_warnings}
    return td, source


def _install(run: Run, td: TrackData, source: dict, *, slot: int | None, filename: str, comment: str, storage_path: str) -> TrackRecord:
    a = td.audio
    record = TrackRecord(
        id=td.id, role=td.role, slot=slot, display_name=td.name, filename=filename, comment=comment,
        storage_path=storage_path, source=source,
        prep={"original_sample_peak_dbfs": a.original_peak_dbfs, "applied_gain_db": a.applied_gain_db,
              "silent": a.silent, "resampled_from": a.resampled_from},
        grid=runs.grid_info(td.features),
    )
    run.tracks = sorted([t for t in run.tracks if t.id != td.id] + [record], key=lambda t: (t.role != "user", t.slot or 0))
    run.section_maps[td.id] = [SectionMap(
        version=1, origin="proposed", bpm=record.grid.bpm, downbeat_s=record.grid.downbeat_s, grid_source="detected",
        sections=[s.to_dict() for s in sections.propose(td)],
    )]
    return record


@router.post("/runs/{run_id}/tracks")
def upload_track(run_id: str, file: UploadFile = File(...), role: str = Form(...), slot: int | None = Form(None),
                 comment: str = Form("")):
    run = _run(run_id)
    tid, display = _track_id(role, slot)
    filename = Path(file.filename or "track").name
    suffix = Path(filename).suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise HTTPException(400, "Please upload a WAV or MP3 file.")
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, TOO_LARGE)
    with runs.lock(run_id):
        _not_running(run)
        if run.state == "Draft":
            runs.set_state(run, "Preparing")
            runs.save(run)

    try:
        td, source = _analyse(tid, display, role, data, suffix)
    except AudioError as e:
        raise HTTPException(422, str(e)) from None

    storage = get_storage()
    # Named by content, so two uploads racing for one slot never overwrite each other's bytes.
    path = f"runs/{run_id}/tracks/{tid}/{source['sha256'][:16]}{suffix}"
    storage.write(path, data, content_type=MEDIA_TYPES[suffix])
    with runs.lock(run_id):
        run = runs.get(run_id)
        _not_running(run)
        old = run.track(tid)
        if old is not None and old.storage_path != path and old.storage_path.startswith("runs/"):
            storage.delete(old.storage_path)
        record = _install(run, td, source, slot=slot, filename=filename, comment=comment[:2000], storage_path=path)
        runs.archive_research(run)
        if run.ready_for_sections() and run.state != "NeedsClarification":
            runs.set_state(run, "ReviewingSections")
        runs.save(run)
    log.info("run %s: %s ready (%s, %.0f s, %.2f BPM)", run_id, tid, filename, source["duration_s"], record.grid.bpm)
    return {"track": record.model_dump(mode="json"), "section_map": run.current_map(tid).model_dump(mode="json")}


@router.delete("/runs/{run_id}/tracks/{track_id}")
def delete_track(run_id: str, track_id: str):
    _run(run_id)
    with runs.lock(run_id):
        run = runs.get(run_id)
        _not_running(run)
        t = _track(run, track_id)
        if t.storage_path.startswith("runs/"):
            get_storage().delete(t.storage_path)
        run.tracks = [x for x in run.tracks if x.id != track_id]
        run.section_maps.pop(track_id, None)
        archived = runs.archive_research(run)
        if not run.ready_for_sections() and run.state in ("ReviewingSections", *lead.FINAL_STATES):
            runs.set_state(run, "Preparing")
        elif archived:
            runs.set_state(run, "ReviewingSections")
        runs.save(run)
    return _payload(run)


def _ranged(data: bytes, media_type: str, range_header: str | None) -> Response:
    """Serve bytes with single-range support so the browser can seek in the audio."""
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if range_header and range_header.startswith("bytes="):
        start_s, _, end_s = range_header[6:].split(",")[0].partition("-")
        try:
            if start_s:
                start, end = int(start_s), int(end_s) if end_s else len(data) - 1
            else:
                start, end = max(0, len(data) - int(end_s)), len(data) - 1
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
        end = min(end, len(data) - 1)
        if start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        return Response(data[start:end + 1], status_code=206, media_type=media_type, headers=headers)
    return Response(data, media_type=media_type, headers=headers)


@router.get("/runs/{run_id}/tracks/{track_id}/audio")
def track_audio(run_id: str, track_id: str, request: Request):
    t = _track(_run(run_id), track_id)
    media_type = MEDIA_TYPES.get(Path(t.filename).suffix.lower(), "application/octet-stream")
    return _ranged(runs.read_source(t.storage_path), media_type, request.headers.get("range"))


@router.get("/runs/{run_id}/tracks/{track_id}/peaks")
def track_peaks(run_id: str, track_id: str):
    run = _run(run_id)
    t = _track(run, track_id)
    storage = get_storage()
    path = peaks.cache_path(t.source["sha256"])
    if storage.exists(path):
        body = storage.read(path)
    else:
        body = json.dumps(peaks.get_or_compute(runs.track_data(run, track_id).audio, t.source["sha256"], storage)).encode()
    return Response(body, media_type="application/json", headers={"Cache-Control": "private, max-age=86400"})


# --- Section maps ---


class SectionIn(BaseModel):
    id: str | None = None
    label: str = Field(min_length=1, max_length=40)
    start_s: float
    end_s: float


class SectionMapIn(BaseModel):
    sections: list[SectionIn] = Field(min_length=1, max_length=64)
    bpm: float = Field(ge=60.0, le=200.0)
    downbeat_s: float
    grid_source: Literal["detected", "user"]
    accepted: bool = False


@router.get("/runs/{run_id}/tracks/{track_id}/sections")
def get_sections(run_id: str, track_id: str):
    run = _run(run_id)
    _track(run, track_id)
    return run.current_map(track_id).model_dump(mode="json")


@router.put("/runs/{run_id}/tracks/{track_id}/sections")
def put_sections(run_id: str, track_id: str, body: SectionMapIn):
    """Save a user-edited map as a new version; user edits are authoritative (SPEC §2.3)."""
    run = _run(run_id)
    t = _track(run, track_id)
    duration = t.source["duration_s"]
    items = sorted(body.sections, key=lambda s: s.start_s)
    if abs(items[0].start_s) > 0.05 or abs(items[-1].end_s - duration) > 0.05:
        raise HTTPException(422, "Sections must cover the whole track.")
    bar_s = 240.0 / body.bpm
    out = []
    for i, s in enumerate(items):
        start = 0.0 if i == 0 else s.start_s
        end = duration if i == len(items) - 1 else s.end_s
        if i and abs(start - out[-1]["end_s"]) > 0.005:
            raise HTTPException(422, "Sections must follow each other without gaps or overlaps.")
        if end - start < 0.5:
            raise HTTPException(422, "Each section must be at least half a second long.")
        out.append({
            "id": s.id or f"s{i + 1}", "label": s.label.strip(), "start_s": round(start, 3), "end_s": round(end, 3),
            **dict(zip(("start_bar", "end_bar"), sections.bar_span(start, end, body.downbeat_s, bar_s))),
            "label_confidence": None,
        })
    with runs.lock(run_id):
        run = runs.get(run_id)
        _not_running(run)
        current = run.current_map(track_id)
        new = SectionMap(version=current.version + 1, origin="user", accepted=body.accepted, bpm=body.bpm,
                         downbeat_s=body.downbeat_s, grid_source=body.grid_source, sections=out)
        run.section_maps[track_id].append(new)
        # Findings measured on the old sections are stale; the new map waits for confirmation.
        if runs.archive_research(run):
            runs.set_state(run, "ReviewingSections")
        runs.save(run)
    return new.model_dump(mode="json")


@router.post("/runs/{run_id}/sections/confirm")
def confirm_sections(run_id: str):
    run = _run(run_id)
    if not run.ready_for_sections():
        raise HTTPException(409, "Upload your track and at least one reference first.")
    with runs.lock(run_id):
        run = runs.get(run_id)
        _not_running(run)
        for maps in run.section_maps.values():
            maps[-1].accepted = maps[-1].confirmed = True
        runs.save(run)
    return _payload(run)


# --- Research progress and figures ---


@router.get("/runs/{run_id}/progress")
def progress(run_id: str, since: int = 0):
    """Trace events from `since` on, and where to ask from next time: the first event still running,
    so the client receives its update when it ends (DESIGN.md §8, §9)."""
    run = _run(run_id)
    events = trace.events(run_id)
    since = max(0, min(since, len(events)))
    window = trace.since(run_id, since)
    running = [since + i for i, e in enumerate(window) if e["status"] == "running"]
    return {"state": run.state, "busy": run_id in runs.active, "stage": lead.stage(run, window),
            "from": since, "events": window, "next": running[0] if running else since + len(window),
            "rounds_used": run.research["rounds_used"], "usage": run.usage}


@router.get("/runs/{run_id}/figures/{figure_id}")
def figure(run_id: str, figure_id: str):
    _run(run_id)
    if not re.fullmatch(r"fig_[0-9a-f]{8}", figure_id):
        raise HTTPException(404, "No such figure.")
    path = f"runs/{run_id}/figures/{figure_id}.png"
    storage = get_storage()
    if not storage.exists(path):
        raise HTTPException(404, "No such figure.")
    return Response(storage.read(path), media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


# --- Demo ---


def _demo_track(entry: dict) -> tuple[dict, TrackData | None, dict | None, str]:
    """One demo track. A frozen track whose analysis is cached needs no download or decode here: its
    record and checked section map come from the corpus, and the audio is decoded when first measured."""
    path = f"demo/audio/{entry['file']}"
    frozen = entry.get("frozen")
    storage = get_storage()
    if frozen and storage.exists(features.cache_path(frozen["sha256"])) and storage.exists(peaks.cache_path(frozen["sha256"])):
        return entry, None, None, path
    tid, display = _track_id(entry["role"], entry.get("slot"))
    data = runs.read_source(path)
    td, source = _analyse(tid, display, entry["role"], data, Path(entry["file"]).suffix.lower())
    return entry, td, source, path


def _install_frozen(run: Run, entry: dict, path: str) -> None:
    f = entry["frozen"]
    tid, display = _track_id(entry["role"], entry.get("slot"))
    record = TrackRecord(id=tid, role=entry["role"], slot=entry.get("slot"), display_name=display,
                         filename=entry["title"] + Path(entry["file"]).suffix, comment=entry.get("comment", ""),
                         storage_path=path, source=f["source"], prep=f["prep"], grid=runs.GridInfo(**f["grid"]))
    run.tracks = sorted([t for t in run.tracks if t.id != tid] + [record], key=lambda t: (t.role != "user", t.slot or 0))
    run.section_maps[tid] = [_frozen_map(f)]


def _frozen_map(f: dict) -> SectionMap:
    """The section map the producer checked for this demo track (scripts/freeze_demo.py)."""
    m = f["map"]
    return SectionMap(version=1, origin="checked", bpm=m["bpm"], downbeat_s=m["downbeat_s"], grid_source=m["grid_source"],
                      sections=[{**s, "label_confidence": None} for s in m["sections"]])


@router.post("/demo")
def create_demo():
    """A run from the demo corpus, so graders get a reproducible run without uploading anything. Tracks
    frozen in the corpus start from the producer's checked section maps."""
    corpus = json.loads((ROOT / "demo" / "corpus.json").read_text())
    entries = [e for e in corpus["tracks"] if e["role"] == "user" or e.get("slot")]
    try:
        with ThreadPoolExecutor(max_workers=len(entries)) as pool:
            prepared = list(pool.map(_demo_track, entries))
    except FileNotFoundError:
        return JSONResponse({"detail": "The demo audio is not available on this server."}, status_code=503)
    run = runs.create(demo=True)
    with runs.lock(run.id):
        run.comments["global"] = corpus.get("global_comment", "")
        for entry, td, source, path in prepared:
            if td is None:
                _install_frozen(run, entry, path)
                continue
            record = _install(run, td, source, slot=entry.get("slot"), filename=entry["title"] + Path(entry["file"]).suffix,
                              comment=entry.get("comment", ""), storage_path=path)
            frozen = entry.get("frozen")
            if frozen and frozen["sha256"] == source["sha256"]:  # analysed again (cache gone), still the checked map
                run.section_maps[record.id] = [_frozen_map(frozen)]
        runs.set_state(run, "Preparing")
        runs.set_state(run, "ReviewingSections")
        runs.save(run)
    return _payload(run)
