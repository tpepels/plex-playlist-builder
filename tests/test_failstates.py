"""End-to-end failure invariants: preserve data, block unsafe operations, recover."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import app as server
import staging
import vpn_gateway as gateway
from state_store import (
    StateCorruption, load_json, write_json,
    validate_jobs_payload, validate_profiles_payload,
)


def sample_jobs(tag):
    return {"version": 1, "jobs": [{"id": tag, "status": "failed"}],
            "archived_jobs": [], "album_submissions": {}}


def test_state_backup_is_last_valid_generation_and_recovers_corruption(tmp_path):
    path = tmp_path / "jobs.json"
    write_json(path, sample_jobs("first"), validate=validate_jobs_payload)
    write_json(path, sample_jobs("second"), validate=validate_jobs_payload)
    assert json.loads((tmp_path / "jobs.json.bak").read_text())["jobs"][0]["id"] == "first"
    path.write_text("{bad", encoding="utf-8")
    saved, restored = load_json(
        path, default={}, validate=validate_jobs_payload,
    )
    assert restored is True
    assert saved["jobs"][0]["id"] == "first"
    assert json.loads(path.read_text()) == saved
    assert len(list(tmp_path.glob("jobs.json.corrupt-*"))) == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_backup_recovers_missing_primary_after_interrupted_update(tmp_path):
    path = tmp_path / "jobs.json"
    write_json(path, sample_jobs("first"), validate=validate_jobs_payload)
    write_json(path, sample_jobs("second"), validate=validate_jobs_payload)
    path.unlink()
    saved, recovered = load_json(path, default={}, validate=validate_jobs_payload)
    assert recovered
    assert saved["jobs"][0]["id"] == "first"
    assert path.exists()


def test_two_corrupt_generations_are_preserved_not_reset(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text("{corrupt", encoding="utf-8")
    backup = tmp_path / "jobs.json.bak"
    backup.write_text('{"jobs":"not-a-list"}', encoding="utf-8")
    with pytest.raises(StateCorruption, match="jobs.json"):
        load_json(path, default={}, validate=validate_jobs_payload)
    assert path.read_text() == "{corrupt"
    assert backup.exists()
    with pytest.raises(StateCorruption):
        write_json(path, sample_jobs("new"), validate=validate_jobs_payload)
    assert path.read_text() == "{corrupt"


def test_write_error_does_not_delete_current_state(tmp_path, monkeypatch):
    import state_store
    path = tmp_path / "jobs.json"
    write_json(path, sample_jobs("safe"), validate=validate_jobs_payload)
    real_replace = os.replace

    def fail_new_primary(source, target):
        if Path(target) == path:
            raise OSError("disk full")
        return real_replace(source, target)

    monkeypatch.setattr(state_store.os, "replace", fail_new_primary)
    with pytest.raises(OSError):
        write_json(path, sample_jobs("later"), validate=validate_jobs_payload)
    assert json.loads(path.read_text())["jobs"][0]["id"] == "safe"
    assert not list(tmp_path.glob(".jobs.json-*"))


def test_profile_index_can_recover_without_exposing_private_keys(tmp_path, monkeypatch):
    meta = tmp_path / "profiles.json"
    monkeypatch.setattr(gateway, "META_FILE", meta)
    first = {"profiles": [{"id": "abc", "filename": "nl.conf",
                           "country": "NL"}], "active": None}
    next_version = {"profiles": [*first["profiles"], {
        "id": "def", "filename": "de.conf", "country": "DE"
    }], "active": None}
    write_json(meta, first, validate=validate_profiles_payload)
    write_json(meta, next_version, validate=validate_profiles_payload)
    meta.write_text("invalid JSON")
    assert gateway.metadata() == first
    assert meta.is_file()
    assert len(list(tmp_path.glob("profiles.json.corrupt-*"))) == 1


def test_damaged_job_history_blocks_all_post_and_stays_readable(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    path.write_text("broken", encoding="utf-8")
    monkeypatch.setattr(server, "JOBS_STATE_FILE", path)
    monkeypatch.setattr(server, "STATE_FAULT", None)
    with pytest.raises(StateCorruption):
        server.load_persisted_jobs()
    monkeypatch.setattr(server, "STATE_FAULT", "Corrupt state; restore backup")
    with server.app.test_client() as client:
        response = client.post("/api/probe", json={
            "url": "https://www.youtube.com/watch?v=test"
        })
        assert response.status_code == 503
        assert response.json["code"] == "state_unavailable"
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json["status"] == "degraded"
        page = client.get("/")
        assert page.status_code == 200
        assert b"persistent-state-alert" in page.data


def test_storage_write_failure_changes_app_to_degraded_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS_STATE_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(server, "STATE_FAULT", None)
    import state_store
    monkeypatch.setattr(state_store.os, "replace",
                        lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with server.jobs_lock:
        with pytest.raises(StateCorruption, match="could not be saved"):
            server.persist_jobs_locked()
    assert server.STATE_FAULT is not None
    with server.app.test_client() as client:
        assert client.post("/api/probe", json={"url":"https://youtube.com"}).status_code == 503


def test_ignored_track_decisions_recover_or_fail_without_erasure(tmp_path, monkeypatch):
    path = tmp_path / "ignored-tracks.json"
    monkeypatch.setattr(server, "IGNORED_TRACKS_FILE", path)
    monkeypatch.setattr(server, "STATE_FAULT", None)
    original = {"youtube:test1": True}
    server.persist_ignored_tracks(original)
    server.persist_ignored_tracks({"youtube:test1": True, "youtube:test2": True})
    path.write_text("broken")
    assert server.load_ignored_tracks() == original
    path.write_text("broken again")
    (tmp_path / "ignored-tracks.json.bak").write_text("also broken")
    with pytest.raises(StateCorruption):
        server.load_ignored_tracks()


def test_crashed_download_does_not_kill_queue_worker(monkeypatch):
    class StopLoop(BaseException):
        pass
    class Queue:
        def __init__(self):
            self.calls = 0
            self.finished = 0
        def get(self):
            self.calls += 1
            if self.calls == 2:
                raise StopLoop
            return "faulty-job"
        def task_done(self):
            self.finished += 1
    q = Queue()
    monkeypatch.setattr(server, "STATE_FAULT", None)
    monkeypatch.setattr(server, "download_queue", q)
    monkeypatch.setattr(server, "run_job",
                        lambda *args: (_ for _ in ()).throw(ValueError("boom")))
    monkeypatch.setattr(server, "persist_jobs_locked", lambda: True)
    with server.jobs_lock:
        previous = server.jobs.get("faulty-job")
        server.jobs["faulty-job"] = {
            "id": "faulty-job", "status": "running", "log": []
        }
    try:
        with pytest.raises(StopLoop):
            server.worker()
        with server.jobs_lock:
            state = server.jobs["faulty-job"]
            assert state["status"] == "failed"
            assert "Unexpected downloader failure" in state["message"]
        assert q.finished == 1
    finally:
        with server.jobs_lock:
            server.jobs.pop("faulty-job", None)
            if previous is not None:
                server.jobs["faulty-job"] = previous


@pytest.mark.parametrize("ident", ["../outside", "../../music", ".", "..",
                                     "a/b", "a\\b", "", "a" * 200])
def test_invalid_staging_paths_never_resolve_outside_tmp(ident, tmp_path):
    with pytest.raises(ValueError, match="Invalid job staging identity"):
        staging.stage_dir(tmp_path, {"staging_owner_id": ident, "id": ident})


def _staging_fixture(tmp_path, monkeypatch, number=2):
    temp = tmp_path / "tmp"
    job = {"id": "safe-job", "source": "yt-dlp", "force": False}
    stage = staging.ensure_stage_dir(temp, job)
    records = []
    for i in range(number):
        media = stage / f"Track-{i} [videoid{i:03d}].opus"
        media.write_bytes(f"staged-{i}".encode())
        records.append({"path": str(media), "artist": "Artist"})
    monkeypatch.setattr(staging, "analyze_stage",
                        lambda *args: {"records": records,
                                       "artist": "Artist", "album": "Album"})
    monkeypatch.setattr(staging, "retag_media", lambda *args, **kw: None)
    return temp, job, stage


def test_copy_failure_preserves_entire_stage_and_cleans_hidden_incoming(tmp_path, monkeypatch):
    temp, job, stage = _staging_fixture(tmp_path, monkeypatch)
    real_copy = staging.shutil.copy2
    count = 0
    def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("disk full during album copy")
        return real_copy(*args)
    monkeypatch.setattr(staging.shutil, "copy2", fail_second)
    with pytest.raises(OSError, match="disk full"):
        staging.publish_stage(temp, tmp_path / "music", job)
    assert len(staging.media_files(stage)) == 2
    assert not list((tmp_path / "music" / ".incoming").glob("*"))


def test_failed_forced_replacement_keeps_old_published_file(tmp_path, monkeypatch):
    temp, job, stage = _staging_fixture(tmp_path, monkeypatch, number=1)
    job["force"] = True
    published = tmp_path / "music" / "Artist" / "Album"
    published.mkdir(parents=True)
    original = published / staging.media_files(stage)[0].name
    original.write_bytes(b"old-published-good-track")
    real_replace = staging.os.replace
    def fail_publish(source, destination):
        if Path(destination) == original:
            raise OSError("rename failed")
        return real_replace(source, destination)
    monkeypatch.setattr(staging.os, "replace", fail_publish)
    with pytest.raises(OSError):
        staging.publish_stage(temp, tmp_path / "music", job)
    assert original.read_bytes() == b"old-published-good-track"
    assert len(staging.media_files(stage)) == 1
    assert not list((tmp_path / "music" / ".incoming").glob("*"))


def test_staging_media_files_ignore_symlinked_outside_audio(tmp_path):
    staging_root = tmp_path / "staging" / "one"
    staging_root.mkdir(parents=True)
    outside = tmp_path / "private.opus"
    outside.write_bytes(b"outside")
    (staging_root / "foreign.opus").symlink_to(outside)
    assert staging.media_files(staging_root) == []
