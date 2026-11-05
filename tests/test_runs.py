"""Run state under concurrency, and the grid a measurement runs on (DESIGN.md §4, §12)."""

import dataclasses
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app import runs
from app.audio import grid
from app.storage import LocalStorage


def test_concurrent_misses_for_one_file_decode_it_once(monkeypatch):
    calls = []

    def slow_prepare(data, suffix):
        calls.append(suffix)
        time.sleep(0.2)
        return SimpleNamespace(duration_s=30.0)

    monkeypatch.setattr(runs, "read_source", lambda path: b"audio")
    monkeypatch.setattr(runs, "prepare_bytes", slow_prepare)
    monkeypatch.setattr(runs.features, "get_or_compute", lambda audio, sha, storage: "features")
    monkeypatch.setattr(runs, "get_storage", lambda: None)
    cache, results = runs._AudioCache(), []
    threads = [threading.Thread(target=lambda: results.append(cache.get("sha", "path", ".wav"))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert len(results) == 4 and all(r == results[0] for r in results)


def test_measurements_use_the_grid_the_user_set(monkeypatch):
    detected = grid.Grid(bpm=136.0, beats_s=np.arange(0.0, 30.0, 60 / 136), downbeat_phase=0,
                         downbeat_confidence="guess", constant_tempo=True, beat_residual_s=0.0)

    @dataclasses.dataclass
    class Features:  # the part of BaseFeatures that track_data touches
        grid: grid.Grid

    audio, feats = SimpleNamespace(duration_s=30.0), Features(detected)
    cache = runs._AudioCache()
    cache.put("sha", audio, feats)
    monkeypatch.setattr(runs, "audio_cache", cache)
    record = runs.TrackRecord(
        id="user", role="user", display_name="Your track", filename="a.wav", storage_path="runs/x/a.wav",
        source={"sha256": "sha"}, prep={},
        grid=runs.GridInfo(bpm=136.0, first_beat_s=0.0, downbeat_s=0.0, downbeat_confidence="guess", constant_tempo=True))
    run = runs.Run(id="x", tracks=[record])
    assert runs.track_data(run, "user").features.grid.bpm == 136.0  # no edits: the detected grid

    run.section_maps["user"] = [runs.SectionMap(version=2, origin="user", bpm=134.0, downbeat_s=0.5,
                                                grid_source="user", sections=[])]
    g = runs.track_data(run, "user").features.grid
    assert g.bpm == 134.0 and g.downbeat_confidence == "user"
    assert g.downbeats_s[0] == pytest.approx(0.5)
    assert feats.grid is detected  # the cached features are not changed


def test_concurrent_writes_to_one_path_do_not_collide(tmp_path):
    storage, errors = LocalStorage(tmp_path), []
    payloads = [bytes([i]) * 1000 for i in range(8)]

    def write(data):
        try:
            for _ in range(20):
                storage.write("cache/features/x.npz", data)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert storage.read("cache/features/x.npz") in payloads
    assert storage.list("cache") == ["cache/features/x.npz"]  # no temporary files left behind
