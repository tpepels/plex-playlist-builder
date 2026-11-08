"""Durable, idempotent Plex scans after a newly published downloader album."""
import asyncio
import json
from pathlib import Path

import pytest
from app import download_plex_sync as sync


def published(*ids):
    return [{"id":value, "request_id":value, "status":"succeeded",
             "final_dir":f"/data/music/YT-DLP Imports/{value}"}
            for value in ids]


def run(coro):
    return asyncio.run(coro)


def test_first_observation_baselines_old_jobs_without_rescanning(tmp_path, monkeypatch):
    calls = []
    async def jobs(*args, **kwargs):
        return published("old-1","old-2")
    monkeypatch.setattr(sync.downloader_bridge, "upstream_json", jobs)
    monkeypatch.setattr(sync, "scan_plex_library", lambda: calls.append("scan"))
    path = tmp_path / "bridge.json"
    result = run(sync.poll_once(state_file=path))
    assert result["baseline"] is True
    assert calls == []
    state = json.loads(path.read_text())
    assert state["seen"] == ["old-1","old-2"]
    assert state["pending"] == []


def test_new_completed_albums_trigger_one_scan_batch_and_deduplicate(
    tmp_path, monkeypatch,
):
    current = published("previous")
    calls = []
    async def jobs(*args, **kwargs):
        return current
    monkeypatch.setattr(sync.downloader_bridge, "upstream_json", jobs)
    monkeypatch.setattr(sync, "scan_plex_library", lambda: calls.append("scan"))
    path = tmp_path / "bridge.json"
    run(sync.poll_once(state_file=path))
    current[:] = published("previous","new-1","new-2")
    result = run(sync.poll_once(state_file=path))
    assert len(calls) == 1
    assert result["pending"] == 0
    state = json.loads(path.read_text())
    assert state["seen"] == ["new-1","new-2","previous"]
    assert state["last_scan"] is not None
    run(sync.poll_once(state_file=path))
    assert len(calls) == 1


def test_failed_plex_scan_keeps_durable_pending_ids_for_retry(tmp_path, monkeypatch):
    current = published("old")
    async def jobs(*args, **kwargs):
        return current
    monkeypatch.setattr(sync.downloader_bridge, "upstream_json", jobs)
    path = tmp_path / "bridge.json"
    run(sync.poll_once(state_file=path))
    current[:] = published("old", "new")
    attempts = []
    def fail_once():
        attempts.append("attempt")
        if len(attempts) == 1:
            raise RuntimeError("Plex unavailable")
    monkeypatch.setattr(sync, "scan_plex_library", fail_once)
    result = run(sync.poll_once(state_file=path))
    assert result["pending"] == 1
    assert result["error"]
    assert json.loads(path.read_text())["pending"] == ["new"]
    result = run(sync.poll_once(state_file=path))
    assert result["pending"] == 0
    assert attempts == ["attempt","attempt"]
    assert json.loads(path.read_text())["pending"] == []


def test_corrupt_plex_scan_history_is_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "bridge.json"
    path.write_text("{invalid")
    async def jobs(*args, **kwargs):
        return published("completed")
    monkeypatch.setattr(sync.downloader_bridge, "upstream_json", jobs)
    monkeypatch.setattr(sync, "scan_plex_library",
                        lambda: pytest.fail("Must not scan after corrupt state"))
    with pytest.raises(RuntimeError, match="damaged"):
        run(sync.poll_once(state_file=path))
    assert path.read_text() == "{invalid"


def test_only_actually_published_succeeded_jobs_are_scanned():
    jobs = [
        {"id":"queued","status":"queued","final_dir":"/music/Album"},
        {"id":"failed","status":"failed","files":["track.opus"]},
        {"id":"missing","status":"succeeded","files":[],"final_dir":""},
        {"id":"done","status":"succeeded","files":["track.opus"]},
        {"id":"done","request_id":"done","status":"succeeded","final_dir":"/music/Album"},
    ]
    assert sync.published_ids(jobs) == {"done"}


def test_sync_does_not_modify_playlist_yaml_or_queue(monkeypatch):
    actions = []
    monkeypatch.setattr(sync, "connect", lambda:
        (object(), type("FakeMusic", (), {"update":lambda self: actions.append("plex-scan")})()))
    sync.scan_plex_library()
    assert actions == ["plex-scan"]


def test_read_only_sync_snapshot_reports_corruption(tmp_path, monkeypatch):
    path = tmp_path / "bridge.json"
    path.write_text("broken")
    monkeypatch.setattr(sync, "STATE_FILE", path)
    result = sync.snapshot()
    assert result["error"]
    assert "damaged" in result["error"]
