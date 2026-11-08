"""Atomic album publication: no partially replaced albums visible to Plex."""
from pathlib import Path

import pytest

import app as server
import staging


def yt_fixture(tmp_path, monkeypatch, *, force=False, n=2):
    temp = tmp_path / "temp"
    stage = temp / "staging" / "job-123"
    stage.mkdir(parents=True)
    records = []
    for i in range(n):
        media = stage / f"Track-{i} [videoid{i:03d}].opus"
        media.write_bytes(f"new-audio-{i}".encode())
        records.append({"path": str(media), "artist": "Artist"})
    monkeypatch.setattr(staging, "analyze_stage",
        lambda *args: {"records": records, "artist": "Artist", "album": "Album"})
    monkeypatch.setattr(staging, "retag_media", lambda *a, **kw: None)
    return temp, {"id":"job-123", "source":"yt-dlp", "force":force}, stage


def test_existing_album_receives_all_new_tracks_as_one_snapshot(tmp_path, monkeypatch):
    temp, job, stage = yt_fixture(tmp_path, monkeypatch)
    album = tmp_path / "music" / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "previous.opus").write_bytes(b"previous-track")
    (album / "cover.jpg").write_bytes(b"existing-custom-art")
    before = list(staging.media_files(album))
    assert len(before) == 1

    result = staging.publish_stage(temp, tmp_path / "music", job)
    assert result["status"] == "published"
    assert len(result["files"]) == 2
    assert (album / "cover.jpg").read_bytes() == b"existing-custom-art"
    assert (album / "previous.opus").read_bytes() == b"previous-track"
    assert len(staging.media_files(album)) == 3
    assert not stage.exists()
    assert not list((tmp_path / "music" / ".incoming").glob("*"))


def test_force_replaces_only_matching_audio_preserving_other_content(tmp_path, monkeypatch):
    temp, job, stage = yt_fixture(tmp_path, monkeypatch, force=True, n=1)
    album = tmp_path / "music" / "Artist" / "Album"
    album.mkdir(parents=True)
    target = album / staging.media_files(stage)[0].name
    target.write_bytes(b"old-audio")
    (album / "unrelated.flac").write_bytes(b"do-not-touch")
    (album / "cover.jpg").write_bytes(b"user-art")
    result = staging.publish_stage(temp, tmp_path / "music", job)
    assert result["status"] == "published"
    assert target.read_bytes() == b"new-audio-0"
    assert (album / "unrelated.flac").read_bytes() == b"do-not-touch"
    assert (album / "cover.jpg").read_bytes() == b"user-art"


def test_existing_matching_tracks_are_not_replaced_without_force(tmp_path, monkeypatch):
    temp, job, stage = yt_fixture(tmp_path, monkeypatch, force=False, n=1)
    album = tmp_path / "music" / "Artist" / "Album"
    album.mkdir(parents=True)
    target = album / staging.media_files(stage)[0].name
    target.write_bytes(b"hand-edited-audio")
    assert staging.publish_stage(temp, tmp_path / "music", job)["status"] == "published"
    assert target.read_bytes() == b"hand-edited-audio"


def test_existing_album_is_untouched_when_snapshot_copy_fails(tmp_path, monkeypatch):
    temp, job, stage = yt_fixture(tmp_path, monkeypatch, n=2)
    album = tmp_path / "music" / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "previous.opus").write_bytes(b"working-copy")
    original = staging.shutil.copy2
    def fail_second_copy(*args, **kwargs):
        if Path(args[0]).name.startswith("Track-1"):
            raise OSError("temporary storage exhausted")
        return original(*args, **kwargs)
    monkeypatch.setattr(staging.shutil, "copy2", fail_second_copy)
    with pytest.raises(OSError, match="storage exhausted"):
        staging.publish_stage(temp, tmp_path / "music", job)
    assert (album / "previous.opus").read_bytes() == b"working-copy"
    assert not (album / "Track-0 [videoid000].opus").exists()
    assert len(staging.media_files(stage)) == 2
    assert not list((tmp_path / "music" / ".incoming").glob("*"))


def test_atomic_exchange_failure_retains_existing_album_and_stage(tmp_path, monkeypatch):
    temp, job, stage = yt_fixture(tmp_path, monkeypatch, force=True, n=1)
    album = tmp_path / "music" / "Artist" / "Album"
    album.mkdir(parents=True)
    target = album / staging.media_files(stage)[0].name
    target.write_bytes(b"old-good-audio")
    def exchange_failure(candidate, existing):
        raise OSError("filesystem does not support rename exchange")
    monkeypatch.setattr(staging, "_exchange_directories", exchange_failure)
    with pytest.raises(OSError, match="does not support"):
        staging.publish_stage(temp, tmp_path / "music", job)
    assert target.read_bytes() == b"old-good-audio"
    assert len(staging.media_files(stage)) == 1
    assert not list((tmp_path / "music" / ".incoming").glob("*"))


def test_spotify_albums_are_committed_as_complete_directories(tmp_path):
    stage = tmp_path / "temp" / "staging" / "spotify1"
    album = stage / "Musician" / "New Release"
    album.mkdir(parents=True)
    (album / "01 - First.opus").write_bytes(b"new-first")
    (album / "02 - Second.opus").write_bytes(b"new-second")
    published = tmp_path / "music" / "Musician" / "New Release"
    published.mkdir(parents=True)
    (published / "01 - First.opus").write_bytes(b"old-first")
    (published / "cover.jpg").write_bytes(b"cover")
    job = {"id":"spotify1", "source":"spotify", "force":True}
    result = staging.publish_stage(tmp_path / "temp", tmp_path / "music", job)
    assert result["status"] == "published"
    assert (published / "01 - First.opus").read_bytes() == b"new-first"
    assert (published / "02 - Second.opus").read_bytes() == b"new-second"
    assert (published / "cover.jpg").read_bytes() == b"cover"
    assert not stage.exists()


def test_deferred_staging_remains_until_durable_publication(tmp_path, monkeypatch):
    temp, job, stage = yt_fixture(tmp_path, monkeypatch)
    result = staging.publish_stage(
        temp, tmp_path / "music", job, defer_cleanup=True,
    )
    assert result["status"] == "published"
    assert len(staging.media_files(stage)) == 2
    assert staging.discard_stage(temp, job) == stage
    assert not stage.exists()


def test_interrupted_finalizing_job_retains_publication_journal(tmp_path):
    raw = {
        "id": "recovery-job", "url": "https://www.youtube.com/playlist?list=test",
        "album_mode": True, "source": "yt-dlp", "status": "finalizing",
        "publication_pending": True, "returncode": 0, "total_items": 2,
        "completed_items": 2, "failed_items": 0, "missing_tracks": [],
    }
    normalized = server.normalize_persisted_job(raw)
    assert normalized["status"] == "failed"
    assert normalized["publication_pending"] is True
    assert normalized["returncode"] == 0


def test_restart_retries_complete_interrupted_album_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(server, "persist_jobs_locked", lambda: True)
    job = {
        "id": "recovery-job", "url": "https://www.youtube.com/playlist?list=test",
        "source": "yt-dlp", "album_mode": True, "status": "failed",
        "publication_pending": True, "returncode": 0,
        "total_items": 2, "failed_items": 0, "missing_tracks": [],
    }
    stage = staging.ensure_stage_dir(tmp_path / "temp", job)
    (stage / "one.opus").write_bytes(b"1")
    (stage / "two.opus").write_bytes(b"2")
    with server.jobs_lock:
        before = dict(server.jobs)
        server.jobs.clear()
        server.jobs[job["id"]] = job
    try:
        restored = server.staged_jobs_to_finalize_after_restore()
        assert restored == [job["id"]]
        assert job["status"] == "succeeded"
        assert job["publication_pending"] is True
        assert len(staging.media_files(stage)) == 2
    finally:
        with server.jobs_lock:
            server.jobs.clear()
            server.jobs.update(before)


def test_incomplete_interrupted_album_stays_out_of_plex(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(server, "persist_jobs_locked", lambda: True)
    job = {
        "id": "recovery-job", "url": "https://www.youtube.com/playlist?list=test",
        "source": "yt-dlp", "album_mode": True, "status": "failed",
        "publication_pending": True, "returncode": 0,
        "total_items": 3, "failed_items": 0, "missing_tracks": [],
    }
    stage = staging.ensure_stage_dir(tmp_path / "temp", job)
    (stage / "one.opus").write_bytes(b"1")
    (stage / "two.opus").write_bytes(b"2")
    with server.jobs_lock:
        before = dict(server.jobs)
        server.jobs.clear()
        server.jobs[job["id"]] = job
    try:
        assert server.staged_jobs_to_finalize_after_restore() == []
        assert job["status"] == "partial"
        assert job["publication_pending"] is False
        assert "3 tracks" not in job["message"] or "2/3" in job["message"]
        assert len(staging.media_files(stage)) == 2
    finally:
        with server.jobs_lock:
            server.jobs.clear()
            server.jobs.update(before)


def test_hidden_publication_directory_cannot_be_symlink(tmp_path, monkeypatch):
    temp, job, stage = yt_fixture(tmp_path, monkeypatch, n=1)
    outside = tmp_path / "outside"
    outside.mkdir()
    music = tmp_path / "music"
    music.mkdir()
    (music / ".incoming").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        staging.publish_stage(temp, music, job)
    assert not list(outside.iterdir())
    assert len(staging.media_files(stage)) == 1



def test_staging_root_or_job_directory_symlinks_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "staging").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        staging.stage_dir(temp, {"id": "safe-id"})
    (temp / "staging").unlink()
    (temp / "staging").mkdir()
    (temp / "staging" / "safe-id").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        staging.stage_dir(temp, {"id": "safe-id"})


def test_existing_album_tree_symlink_is_not_followed(tmp_path, monkeypatch):
    temp, job, stage = yt_fixture(tmp_path, monkeypatch, n=1)
    music = tmp_path / "music"
    external = tmp_path / "external"
    external.mkdir()
    (external / "important.opus").write_bytes(b"external-untouched")
    (music / "Artist").mkdir(parents=True)
    (music / "Artist" / "Album").symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlinked library album"):
        staging.publish_stage(temp, music, job)
    assert (external / "important.opus").read_bytes() == b"external-untouched"
    assert len(staging.media_files(stage)) == 1



def test_publication_persists_success_before_discarding_staging(tmp_path, monkeypatch):
    temp, staging_job, stage = yt_fixture(tmp_path, monkeypatch, n=1)
    job = {
        **staging_job, "url": "https://www.youtube.com/watch?v=videoid000",
        "status": "succeeded", "album_mode": False,
        "metadata_artist": "Artist", "metadata_album": "Album",
        "completed_items": 1, "total_items": 1, "returncode": 0,
        "files": [], "log": [], "failed_items": 0,
    }
    monkeypatch.setattr(server, "TEMP_DIR", temp)
    monkeypatch.setattr(server, "IMPORT_ROOT", tmp_path / "music")
    observed = []
    def persist():
        observed.append(("persist", job["status"],
                         bool(job.get("publication_pending")), stage.exists()))
        return True
    monkeypatch.setattr(server, "persist_jobs_locked", persist)
    original_discard = staging.discard_stage
    def discard(temp_root, snapshot):
        assert any(event[0] == "persist" and event[1] == "succeeded"
                   and event[2] is False for event in observed)
        assert stage.exists()
        observed.append(("discard",))
        return original_discard(temp_root, snapshot)
    monkeypatch.setattr(staging, "discard_stage", discard)
    monkeypatch.setattr(server, "schedule_library_change_nudge", lambda: None)
    with server.jobs_lock:
        existing = dict(server.jobs)
        server.jobs.clear()
        server.jobs[job["id"]] = job
    try:
        result = server.finalize_staged_job(job["id"])
        assert result["status"] == "published"
        assert job["status"] == "succeeded"
        assert job["publication_pending"] is False
        assert not stage.exists()
        assert observed[-1] == ("discard",)
    finally:
        with server.jobs_lock:
            server.jobs.clear()
            server.jobs.update(existing)


def test_publication_state_write_failure_keeps_stage_for_crash_replay(tmp_path, monkeypatch):
    temp, staging_job, stage = yt_fixture(tmp_path, monkeypatch, n=1)
    job = {
        **staging_job, "url": "https://www.youtube.com/watch?v=videoid000",
        "status": "succeeded", "album_mode": False,
        "metadata_artist": "Artist", "metadata_album": "Album",
        "completed_items": 1, "total_items": 1, "returncode": 0,
        "files": [], "log": [], "failed_items": 0,
    }
    monkeypatch.setattr(server, "TEMP_DIR", temp)
    monkeypatch.setattr(server, "IMPORT_ROOT", tmp_path / "music")
    persist_count = 0
    def fail_after_files_published():
        nonlocal persist_count
        persist_count += 1
        if persist_count == 2:
            raise OSError("state volume full after album commit")
        return True
    monkeypatch.setattr(server, "persist_jobs_locked", fail_after_files_published)
    monkeypatch.setattr(server, "schedule_library_change_nudge", lambda: None)
    with server.jobs_lock:
        existing = dict(server.jobs)
        server.jobs.clear()
        server.jobs[job["id"]] = job
    try:
        with pytest.raises(OSError, match="state volume full"):
            server.finalize_staged_job(job["id"])
        assert (tmp_path / "music" / "Artist" / "Album").is_dir()
        assert len(staging.media_files(stage)) == 1
    finally:
        with server.jobs_lock:
            server.jobs.clear()
            server.jobs.update(existing)
