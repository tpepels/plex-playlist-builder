"""Small tests for failed-download permanence and 403 explanations."""
import json

import app as server
from job_history import history_records


def failure(job_id, created=10):
    return {
        "id": job_id, "url": "https://music.youtube.com/playlist?list=OLAK5uy_test",
        "source": "yt-dlp", "status": "failed", "http_403": True,
        "created_at": created, "finished_at": created + 3,
        "source_title": "Beethoven: Piano Sonatas", "source_album_artist": "Pianist",
        "message": "YouTube returned HTTP 403", "completed_items": 0,
        "total_items": 11, "files": [], "final_dir": None,
        "log": ["WARNING: YouTube Music is not directly supported",
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"],
    }


def test_old_403_failure_is_searchable_even_when_removed_from_active_queue():
    old = failure("old403")
    result = history_records({}, {"old403": {**old, "removed_from_queue": True}},
                             query="Beethoven", status="failed")
    assert result["total"] == 1
    row = result["items"][0]
    assert row["http_403"]
    assert "403" in row["reason"]
    assert row["removed_from_queue"] is True


def test_every_attempt_is_retained_and_history_is_paginated():
    jobs = {f"job-{i}": failure(f"job-{i}", created=i) for i in range(90)}
    data = history_records(jobs, {}, status="failed", offset=10, limit=20)
    assert data["total"] == 90
    assert len(data["items"]) == 20
    assert data["items"][0]["job_id"] == "job-79"


def test_job_state_writer_preserves_older_failure_without_pruning(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS_STATE_FILE", tmp_path / "jobs.json")
    with server.jobs_lock:
        old_jobs = dict(server.jobs)
        old_archive = dict(server.archived_jobs)
        old_claims = dict(server.album_submissions)
        try:
            server.jobs.clear()
            server.archived_jobs.clear()
            server.album_submissions.clear()
            server.jobs.update({f"job-{i}": failure(f"job-{i}", i) for i in range(65)})
            server.archived_jobs["removed"] = {
                **failure("removed", 0),
                "removed_from_queue": True, "archived_at": 150,
            }
            assert server.persist_jobs_locked()
        finally:
            server.jobs.clear()
            server.jobs.update(old_jobs)
            server.archived_jobs.clear()
            server.archived_jobs.update(old_archive)
            server.album_submissions.clear()
            server.album_submissions.update(old_claims)
    state = json.loads((tmp_path / "jobs.json").read_text())
    assert len(state["jobs"]) == 65
    assert len(state["archived_jobs"]) == 1
    assert any(item["id"] == "job-0" for item in state["jobs"])
    assert server.MAX_HISTORY == 0


def test_download_history_endpoint_filters_and_reports_403(tmp_path, monkeypatch):
    with server.jobs_lock:
        original = dict(server.jobs)
        originals_archive = dict(server.archived_jobs)
        server.jobs.clear()
        server.archived_jobs.clear()
        server.jobs["403-case"] = failure("403-case")
    try:
        client = server.app.test_client()
        response = client.get("/api/download-history?q=Beethoven&status=failed")
        assert response.status_code == 200
        data = response.get_json()
        assert data["total"] == 1
        assert "403" in data["items"][0]["reason"]
        assert data["items"][0]["job_id"] == "403-case"
    finally:
        with server.jobs_lock:
            server.jobs.clear()
            server.jobs.update(original)
            server.archived_jobs.clear()
            server.archived_jobs.update(originals_archive)
