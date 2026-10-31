"""Short, network-free regression tests for automatic 403 recovery."""
from __future__ import annotations

import json

import pytest

import app as server
from download_resilience import (
    player_client_for_attempt, recovery_policy, retry_delay,
    should_restart, track_pacing_args,
)


def test_bounded_policy_and_recovery_client_selection():
    policy = recovery_policy({
        "YOUTUBE_403_RESTARTS": "2",
        "YOUTUBE_403_BACKOFF_SECONDS": "30",
        "YOUTUBE_TRACK_PAUSE_MIN": "5",
        "YOUTUBE_TRACK_PAUSE_MAX": "10",
        "YOUTUBE_REQUEST_PAUSE": "1",
    })
    assert retry_delay(1, policy) == 30
    assert retry_delay(2, policy) == 90
    assert player_client_for_attempt("mweb", 0, policy) == "mweb"
    assert player_client_for_attempt("mweb", 1, policy) == "mweb"
    assert player_client_for_attempt("mweb", 2, policy) == "web_safari"
    assert "--sleep-requests" in track_pacing_args(policy)
    assert "--sleep-interval" in track_pacing_args(policy)


def test_does_not_retry_forced_downloads_or_non403_errors():
    policy = recovery_policy({})
    args = dict(youtube=True, spotify=False, forced=False,
                returncode=1, got_http_403=True,
                already_restarted=0, policy=policy)
    assert should_restart(**args)
    assert not should_restart(**{**args, "forced": True})
    assert not should_restart(**{**args, "got_http_403": False})
    assert not should_restart(**{**args, "youtube": False})
    assert not should_restart(**{**args, "returncode": 0})
    assert not should_restart(**{**args, "already_restarted": 2})


def test_retries_and_waits_are_bounded_even_for_bad_environment():
    p = recovery_policy({
        "YOUTUBE_403_RESTARTS": "500",
        "YOUTUBE_403_BACKOFF_SECONDS": "99999",
        "YOUTUBE_TRACK_PAUSE_MAX": "-2",
        "YOUTUBE_TRACK_PAUSE_MIN": "60",
    })
    assert p["restarts"] == 3
    assert retry_delay(3, p) <= 900
    assert p["track_pause_min"] == 30


@pytest.fixture
def isolated_job(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS_STATE_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(server, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(server, "ARCHIVE_FILE", tmp_path / "archive.txt")
    job = {
        "id": "fake-403", "url": "https://music.youtube.com/playlist?list=OLAK5uy_test",
        "status": "queued", "playlist": True, "album_mode": True,
        "collection_mode": True, "compilation": False, "force": False,
        "source": "yt-dlp", "created_at": 1, "attempt": 1,
        "completed_items": 0, "total_items": 2, "files": [], "log": [],
        "failed_items": 0, "missing_tracks": [], "staging_owner_id": "fake-403",
        "source_title": "Test album", "source_album_artist": "Test artist",
        "http_403": False, "returncode": None,
    }
    with server.jobs_lock:
        originals = dict(server.jobs)
        claims = dict(server.album_submissions)
        archive = dict(server.archived_jobs)
        server.jobs.clear()
        server.album_submissions.clear()
        server.archived_jobs.clear()
        server.jobs[job["id"]] = job
    try:
        yield job
    finally:
        with server.jobs_lock:
            server.jobs.clear()
            server.jobs.update(originals)
            server.album_submissions.clear()
            server.album_submissions.update(claims)
            server.archived_jobs.clear()
            server.archived_jobs.update(archive)


class FakeProcess:
    def __init__(self, error):
        self.stdout = iter(
            ["ERROR: unable to download video data: HTTP Error 403: Forbidden\n"]
            if error else []
        )
        self.exit = 1 if error else 0

    def wait(self):
        return self.exit


def test_one_http403_restarts_fresh_process_and_can_recover(
    isolated_job, monkeypatch
):
    policy = recovery_policy({
        "YOUTUBE_403_RESTARTS": "1",
        "YOUTUBE_403_BACKOFF_SECONDS": "5",
        "YOUTUBE_403_FALLBACK_CLIENT": "web_safari",
    })
    monkeypatch.setattr(server, "recovery_policy", lambda: policy)
    sleep_calls, cmd_calls = [], []
    monkeypatch.setattr(server.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    def process(cmd, **kwargs):
        cmd_calls.append(cmd)
        return FakeProcess(error=len(cmd_calls) == 1)

    monkeypatch.setattr(server.subprocess, "Popen", process)
    server.run_job(isolated_job["id"])
    assert len(cmd_calls) == 2
    assert sleep_calls == [5]
    assert "youtube:player_client=mweb" in cmd_calls[0]
    assert "youtube:player_client=web_safari" in cmd_calls[1]
    assert isolated_job["automatic_403_restarts"] == 1
    assert isolated_job["recovered_http_403"] is True
    assert isolated_job["http_403"] is False
    assert isolated_job["status"] == "succeeded"
    assert any("RECOVERY:" in line for line in isolated_job["log"])
    saved = json.loads(server.JOBS_STATE_FILE.read_text())
    assert saved["jobs"][0]["automatic_403_restarts"] == 1


def test_persistent_403_fails_after_bounded_restarts(
    isolated_job, monkeypatch
):
    policy = recovery_policy({
        "YOUTUBE_403_RESTARTS": "2",
        "YOUTUBE_403_BACKOFF_SECONDS": "5",
    })
    monkeypatch.setattr(server, "recovery_policy", lambda: policy)
    sleeps, cmd_calls = [], []
    monkeypatch.setattr(server.time, "sleep", lambda seconds: sleeps.append(seconds))

    def process(cmd, **kwargs):
        cmd_calls.append(cmd)
        return FakeProcess(error=True)

    monkeypatch.setattr(server.subprocess, "Popen", process)
    server.run_job(isolated_job["id"])
    assert len(cmd_calls) == 3
    assert sleeps == [5, 15]
    assert isolated_job["status"] == "failed"
    assert isolated_job["http_403"] is True
    assert isolated_job["automatic_403_restarts"] == 2
    assert "after 2 automatic restart" in isolated_job["message"]


def test_other_failure_never_triggers_403_recovery(isolated_job, monkeypatch):
    monkeypatch.setattr(server, "recovery_policy", lambda: recovery_policy({}))
    calls = []

    class OtherFailure:
        stdout = iter(["ERROR: unavailable recording\n"])
        def wait(self):
            return 1

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: (
        calls.append("run") or OtherFailure()
    ))
    monkeypatch.setattr(server.time, "sleep", lambda seconds: pytest.fail("Unexpected sleep"))
    server.run_job(isolated_job["id"])
    assert calls == ["run"]
    assert isolated_job["status"] == "failed"
