"""Short, offline regression tests for bulk retry of distinct failed albums."""
from __future__ import annotations

import queue

import pytest

from retry_queue import failed_candidates, source_key, build_retry


ALBUM = "https://music.youtube.com/playlist?list=OLAK5uy_testAlbum"
OTHER = "https://music.youtube.com/playlist?list=OLAK5uy_anotherAlbum"


def job(identifier, *, url=ALBUM, status="failed", attempt=1, created=1,
        completed=5, total=6, retry_of=None, final_dir=None, source="yt-dlp"):
    return {
        "id": identifier, "url": url, "playlist": True,
        "collection_mode": True, "album_mode": True, "compilation": False,
        "source": source, "status": status, "attempt": attempt,
        "created_at": created, "retry_of": retry_of,
        "completed_items": completed, "total_items": total,
        "files": ["/data/tmp/staging/origin/01.opus"], "log": [],
        "staging_owner_id": "origin",
        "source_title": "Album " + identifier,
        "source_album_artist": "Artist",
        "final_dir": final_dir,
        "missing_tracks": [],
    }


def test_selection_excludes_recovered_and_active_sources_even_if_old_attempt_failed():
    records = {
        "x": job("x", created=1),
        "y": job("y", status="succeeded", attempt=2, created=2, retry_of="x",
                 final_dir="/data/music/Artist/Album"),
        "a": job("a", url=OTHER, created=3),
        "b": job("b", url=OTHER, status="running", created=4),
    }
    selected, skipped = failed_candidates(records)
    assert selected == []
    assert skipped == {"already_succeeded": 1, "already_active": 1}


def test_dedup_same_youtube_album_across_youtube_music_and_regular_youtube():
    youtube = "https://www.youtube.com/playlist?list=OLAK5uy_testAlbum&si=tracking"
    assert source_key(job("a")) == source_key(job("b", url=youtube))
    records = {
        "root": job("root", created=1),
        "retry": job("retry", url=youtube, attempt=2, created=2, retry_of="root"),
    }
    selected, excluded = failed_candidates(records)
    assert [item["id"] for item in selected] == ["retry"]


def test_archived_success_prevents_bulk_retry_after_another_failed_attempt():
    selected, skipped = failed_candidates(
        {"failed": job("failed", created=5)},
        {"succeeded": job("succeeded", status="succeeded", created=1)},
    )
    assert selected == []
    assert skipped["already_succeeded"] == 1


def test_removed_failed_job_is_not_reintroduced_and_published_partial_is_excluded():
    records = {"published": job("published", url=OTHER, status="partial",
                                final_dir="/data/music/Some Album")}
    archive = {"removed": job("removed", url=ALBUM, created=4)}
    selected, exclusions = failed_candidates(records, archive)
    assert selected == []
    assert exclusions["removed_from_queue"] == 1
    assert exclusions["already_published"] == 1


def test_build_retry_preserves_audio_stage_and_archive_safety():
    source = job("root", attempt=4, completed=68, total=69)
    source["album_submission_key"] = "key"
    source["match_audit"] = {"requested_album": "Test"}
    retry = build_retry(source, "new-id", created_at=10)
    assert retry["id"] == "new-id"
    assert retry["retry_of"] == "root"
    assert retry["status"] == "queued"
    assert retry["force"] is False
    assert retry["staging_owner_id"] == "origin"
    assert retry["files"] == source["files"]
    assert retry["completed_items"] == 68
    assert retry["attempt"] == 5
    assert retry["match_audit"] == source["match_audit"]
    assert retry["album_submission_key"] == "key"


@pytest.fixture
def server(tmp_path, monkeypatch):
    import app as service
    monkeypatch.setattr(service, "JOBS_STATE_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(service, "download_queue", queue.Queue(maxsize=2))
    with service.jobs_lock:
        orig_jobs = dict(service.jobs)
        orig_archive = dict(service.archived_jobs)
        orig_claims = dict(service.album_submissions)
        service.jobs.clear()
        service.archived_jobs.clear()
        service.album_submissions.clear()
    try:
        yield service
    finally:
        with service.jobs_lock:
            service.jobs.clear()
            service.jobs.update(orig_jobs)
            service.archived_jobs.clear()
            service.archived_jobs.update(orig_archive)
            service.album_submissions.clear()
            service.album_submissions.update(orig_claims)


def test_bulk_enqueues_once_per_failed_source_and_is_idempotent(server):
    with server.jobs_lock:
        server.jobs.update({
            "root": job("root", created=1, completed=2),
            "latest": job("latest", created=2, attempt=2, completed=5,
                          retry_of="root"),
            "other": job("other", url=OTHER, created=3, completed=3),
        })
    client = server.app.test_client()
    preview = client.get("/api/jobs/retry-failed/preview").get_json()
    assert preview["eligible"] == 2
    assert preview["will_queue"] == 2
    first = client.post("/api/jobs/retry-failed")
    assert first.status_code == 202
    result = first.get_json()
    assert result["queued_count"] == 2
    assert set(item["retry_of"] for item in result["queued"]) == {"latest", "other"}
    assert server.download_queue.qsize() == 2
    retry = server.jobs[next(item["job_id"] for item in result["queued"]
                             if item["retry_of"] == "latest")]
    assert retry["staging_owner_id"] == "origin"
    assert retry["force"] is False
    assert retry["completed_items"] == 5

    again = client.post("/api/jobs/retry-failed")
    assert again.status_code == 200
    assert again.get_json()["queued_count"] == 0
    assert server.download_queue.qsize() == 2
    assert client.get("/api/jobs/retry-failed/preview").get_json()["eligible"] == 0


def test_capacity_queues_available_slots_and_reports_leftovers(server):
    with server.jobs_lock:
        server.jobs.update({
            "a": job("a", url=ALBUM, completed=5, created=1),
            "b": job("b", url=OTHER, completed=3, created=2),
            "c": job("c", url="https://music.youtube.com/playlist?list=OLAK5uy_third",
                     completed=4, created=3),
        })
    client = server.app.test_client()
    preview = client.get("/api/jobs/retry-failed/preview").get_json()
    assert preview["will_queue"] == 2
    assert preview["skipped_capacity"] == 1
    result = client.post("/api/jobs/retry-failed").get_json()
    assert result["queued_count"] == 2
    assert result["skipped_capacity"] == 1
    assert {item["retry_of"] for item in result["queued"]} == {"a", "c"}
    assert server.download_queue.qsize() == 2
    # No third job was created accidentally.
    assert len(server.jobs) == 5


def test_full_queue_adds_nothing_and_retains_original_records(server):
    server.download_queue.put_nowait("inflight")
    server.download_queue.put_nowait("pending")
    with server.jobs_lock:
        server.jobs["failed"] = job("failed")
    response = server.app.test_client().post("/api/jobs/retry-failed")
    assert response.status_code == 200
    assert response.get_json()["queued_count"] == 0
    assert response.get_json()["skipped_capacity"] == 1
    assert list(server.jobs) == ["failed"]


def test_single_job_retry_reuses_same_safe_builder(server):
    with server.jobs_lock:
        server.jobs["single"] = job("single")
    response = server.app.test_client().post("/api/jobs/single/retry")
    assert response.status_code == 202
    retry_id = response.get_json()["job_id"]
    assert server.jobs[retry_id]["retry_of"] == "single"
    assert server.jobs[retry_id]["staging_owner_id"] == "origin"
    assert server.jobs[retry_id]["force"] is False


def test_legacy_retry_without_staging_owner_reuses_original_job_folder():
    previous = job("original-legacy", completed=68, total=69)
    previous.pop("staging_owner_id")
    retry = build_retry(previous, "new-retry", created_at=100)
    assert retry["staging_owner_id"] == "original-legacy"
    assert retry["retry_of"] == "original-legacy"
