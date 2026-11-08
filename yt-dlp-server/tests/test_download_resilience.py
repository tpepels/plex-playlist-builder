"""Short, network-free regression tests for automatic 403 recovery."""
from __future__ import annotations

import json

import pytest

import app as server
from download_resilience import (
    player_client_for_attempt, recovery_policy, retry_delay,
    should_restart, track_pacing_args, needs_vpn_rotation,
    youtube_failure_kind, HTTP_403, BOT_CHALLENGE,
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
    assert player_client_for_attempt("mweb", 2, policy) == "default"
    assert "--sleep-requests" in track_pacing_args(policy)
    assert "--sleep-interval" in track_pacing_args(policy)


def test_does_not_retry_forced_downloads_or_non403_errors():
    policy = recovery_policy({})
    args = dict(youtube=True, spotify=False, forced=False,
                returncode=1, failure_kind=HTTP_403,
                already_restarted=0, policy=policy)
    assert should_restart(**args)
    assert not should_restart(**{**args, "forced": True})
    assert not should_restart(**{**args, "failure_kind": None})
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
        "status": "queued", "playlist": True, "album_mode": False,
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
        "YOUTUBE_403_FALLBACK_CLIENT": "default",
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
    assert "youtube:player_client=default" in cmd_calls[1]
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


def test_bot_challenge_and_http_403_share_classifier():
    assert youtube_failure_kind(
        "ERROR: [youtube] IzsJqQ31gA4: Sign in to confirm you’re not a bot."
    ) == BOT_CHALLENGE
    assert youtube_failure_kind("ERROR: HTTP Error 403: Forbidden") == HTTP_403
    assert youtube_failure_kind("ERROR: private recording") is None


def test_bot_rotation_immediate_but_generic_403_rotation_final():
    policy = recovery_policy({})
    assert needs_vpn_rotation(BOT_CHALLENGE, 1, policy)
    assert not needs_vpn_rotation(HTTP_403, 1, policy)
    assert needs_vpn_rotation(HTTP_403, policy["restarts"], policy)
    assert should_restart(youtube=True, spotify=False, forced=False, returncode=1,
                          failure_kind=BOT_CHALLENGE, already_restarted=0, policy=policy)


def test_bot_challenge_rotates_before_fresh_process(isolated_job, monkeypatch):
    policy = recovery_policy({"YOUTUBE_403_RESTARTS": "1",
                              "YOUTUBE_403_BACKOFF_SECONDS": "5"})
    monkeypatch.setattr(server, "recovery_policy", lambda: policy)
    events = []
    monkeypatch.setattr(server.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    monkeypatch.setattr(server, "rotate_if_enabled", lambda: (
        events.append(("rotate",)) or
        {"changed": True, "new_ip": "198.51.100.22", "country": "Sweden"}
    ))
    class BotProcess:
        def __init__(self, fail):
            events.append(("spawn", fail))
            self.stdout = iter([
                "ERROR: [youtube] IzsJqQ31gA4: Sign in to confirm you're not a bot.\n"
            ] if fail else [])
            self.exit = 1 if fail else 0
        def wait(self): return self.exit
    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda *a, **kw: BotProcess(
                            len([e for e in events if e[0] == "spawn"]) == 0))
    server.run_job(isolated_job["id"])
    assert events == [("spawn", True), ("rotate",), ("sleep", 5), ("spawn", False)]
    assert isolated_job["status"] == "succeeded"
    assert isolated_job["automatic_bot_restarts"] == 1
    assert isolated_job["automatic_vpn_rotations"] == 1
    assert isolated_job["recovered_bot_challenge"] is True


def test_bot_challenge_never_retries_unchanged_ip(isolated_job, monkeypatch):
    monkeypatch.setattr(server, "recovery_policy",
                        lambda: recovery_policy({"YOUTUBE_403_RESTARTS": "2"}))
    monkeypatch.setattr(server, "rotate_if_enabled",
                        lambda: {"changed": False, "error": "no new IP"})
    calls = []
    class BotProcess:
        stdout = iter([
            "ERROR: [youtube] IzsJqQ31gA4: Sign in to confirm you're not a bot.\n"
        ])
        def wait(self): return 1
    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda *a, **kw: calls.append(1) or BotProcess())
    monkeypatch.setattr(server.time, "sleep",
                        lambda seconds: pytest.fail("Blocked IP must not be retried"))
    server.run_job(isolated_job["id"])
    assert calls == [1]
    assert isolated_job["status"] == "failed"
    assert isolated_job.get("automatic_vpn_rotations", 0) == 0
    assert "no usable alternative VPN exit" in isolated_job["message"]


def test_initial_download_direct_then_temporary_vpn_then_direct(isolated_job, monkeypatch):
    policy = recovery_policy({"YOUTUBE_403_RESTARTS": "1",
                              "YOUTUBE_403_BACKOFF_SECONDS": "5"})
    monkeypatch.setattr(server, "recovery_policy", lambda: policy)
    events = []
    monkeypatch.setattr(server, "direct_if_enabled", lambda: (
        events.append("direct") or {"direct": True}
    ))
    monkeypatch.setattr(server, "rotate_if_enabled", lambda: (
        events.append("vpn") or
        {"changed": True, "new_ip": "198.51.100.28", "country": "Sweden"}
    ))
    monkeypatch.setattr(server.time, "sleep", lambda seconds: events.append("delay"))
    class Process:
        def __init__(self):
            events.append("spawn")
            first = events.count("spawn") == 1
            self.stdout = iter([
                "ERROR: [youtube] IzsJqQ31gA4: Sign in to confirm you're not a bot.\n"
            ] if first else [])
            self.code = 1 if first else 0
        def wait(self):
            return self.code
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: Process())
    server.run_job(isolated_job["id"])
    assert events == ["direct", "spawn", "vpn", "delay",
                      "spawn", "direct", "direct"]
    assert isolated_job["status"] == "succeeded"
    assert isolated_job["automatic_vpn_rotations"] == 1
    assert any("direct connection restored" in entry for entry in isolated_job["log"])


def test_unrelated_yt_dlp_error_never_activates_vpn(isolated_job, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "direct_if_enabled", lambda: (
        calls.append("direct") or {"direct": True}
    ))
    monkeypatch.setattr(server, "rotate_if_enabled",
                        lambda: pytest.fail("VPN must stay disconnected"))
    class Process:
        stdout = iter(["ERROR: private video\n"])
        def wait(self): return 1
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: Process())
    server.run_job(isolated_job["id"])
    assert isolated_job["status"] == "failed"
    assert calls == ["direct", "direct", "direct"]


def test_unexpected_subprocess_error_restores_direct(isolated_job, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "direct_if_enabled", lambda: (
        calls.append("direct") or {"direct": True}
    ))
    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("spawn failed")))
    server.run_job(isolated_job["id"])
    assert isolated_job["status"] == "failed"
    assert calls == ["direct", "direct"]


def test_failed_direct_precheck_disallows_download(isolated_job, monkeypatch):
    monkeypatch.setattr(server, "direct_if_enabled",
                        lambda: {"direct": False, "error": "blocked"})
    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda *a, **kw: pytest.fail("No downloader must start"))
    server.run_job(isolated_job["id"])
    assert isolated_job["status"] == "failed"
    assert "blocked" in isolated_job["message"].lower()


def test_failed_vpn_teardown_does_not_publish_success(isolated_job, monkeypatch):
    states = iter([{"direct": True}, {"direct": False}, {"direct": False}])
    monkeypatch.setattr(server, "direct_if_enabled", lambda: next(states))
    class Process:
        stdout = iter([])
        def wait(self): return 0
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: Process())
    server.run_job(isolated_job["id"])
    assert isolated_job["status"] == "failed"
    assert "gateway" in isolated_job["message"].lower()


def test_downloader_restart_clears_surviving_gateway_before_queue(monkeypatch):
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    events = []
    monkeypatch.setattr(server, "direct_if_enabled", lambda: (
        events.append("direct") or {"direct": True}
    ))
    monkeypatch.setattr(server, "load_persisted_jobs", lambda: (
        events.append("restore_queue") or []
    ))
    monkeypatch.setattr(server, "staged_jobs_to_finalize_after_restore",
                        lambda: [])
    monkeypatch.setattr(server, "schedule_library_change_nudge",
                        lambda **kw: None)

    class NoStartThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            events.append("start_worker")
    monkeypatch.setattr(server.threading, "Thread", NoStartThread)
    server.start_runtime_workers()
    assert events[0:2] == ["direct", "restore_queue"]
    assert events.count("start_worker") == 2


def test_gateway_startup_failure_keeps_dashboard_available(monkeypatch, capsys):
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    events = []
    monkeypatch.setattr(server, "direct_if_enabled",
                        lambda: {"direct": False, "error": "offline"})
    monkeypatch.setattr(server, "load_persisted_jobs", lambda: (
        events.append("restore_queue") or []
    ))
    monkeypatch.setattr(server, "staged_jobs_to_finalize_after_restore",
                        lambda: [])
    monkeypatch.setattr(server, "schedule_library_change_nudge",
                        lambda **kw: None)
    class NoStartThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
    monkeypatch.setattr(server.threading, "Thread", NoStartThread)
    server.start_runtime_workers()
    assert events == ["restore_queue"]
    assert "will fail closed" in capsys.readouterr().out



def test_silent_zero_of_twelve_album_has_specific_durable_cause(
    isolated_job, monkeypatch,
):
    isolated_job["album_mode"] = True
    isolated_job["total_items"] = 12
    isolated_job["source_title"] = "Cruel Joke"
    class EmptyPlaylist:
        stdout = iter([
            "WARNING: [youtube:tab] YouTube Music is not directly supported. "
            "Redirecting to https://www.youtube.com/playlist?list=OLAK5uy_example\n"
        ])
        def wait(self):
            return 0
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: EmptyPlaylist())
    server.run_job(isolated_job["id"])

    assert isolated_job["status"] == "failed"
    assert isolated_job["returncode"] == 0
    assert isolated_job["failure_code"] == "no_playlist_entries"
    assert "expected 12" in isolated_job["failure_reason"]
    assert "Cause:" in isolated_job["message"]
    assert "exit code 0" in isolated_job["failure_evidence"]
    assert "0/12" in isolated_job["message"]
    grouped = {**isolated_job, "progress": "", "current_item": 0,
               "retry_of": None}
    with server.jobs_lock:
        card = server.public_job_from_group_locked(isolated_job["id"], [grouped])
    assert card["diagnosis"]["failure_code"] == "no_playlist_entries"
    assert card["diagnosis"]["next_action"] == isolated_job["failure_action"]


def test_archive_only_zero_of_twelve_is_not_mislabeled_as_empty(
    isolated_job, monkeypatch,
):
    isolated_job["album_mode"] = True
    isolated_job["total_items"] = 12
    class ArchiveOnly:
        stdout = iter(
            [f"[download] track-{i} has already been recorded in the archive\n"
             for i in range(12)]
        )
        def wait(self):
            return 0
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: ArchiveOnly())
    server.run_job(isolated_job["id"])
    assert isolated_job["status"] == "failed"
    assert isolated_job["failure_code"] == "archive_without_staging"
    assert isolated_job["archive_skipped_items"] == 12
    assert "music library" in isolated_job["failure_action"]


def test_extractor_error_survives_bounded_log_rotation(
    isolated_job, monkeypatch,
):
    isolated_job["album_mode"] = True
    isolated_job["total_items"] = 12
    class ErrorProcess:
        stdout = iter(
            ["ERROR: [youtube:tab] Playlist is not available\n"]
            + [f"debug progress {i}\n" for i in range(55)]
        )
        def wait(self):
            return 1
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: ErrorProcess())
    server.run_job(isolated_job["id"])
    assert isolated_job["status"] == "failed"
    assert isolated_job["returncode"] == 1
    assert len(isolated_job["log"]) <= 40
    assert not any("Playlist is not available" in line for line in isolated_job["log"])
    assert "Playlist is not available" in isolated_job["failure_reason"]
    assert isolated_job["failure_code"] == "extractor_error"


def test_failure_cause_survives_job_reload_and_retry_reset(
    isolated_job, monkeypatch,
):
    isolated_job.update(album_mode=True, total_items=12)
    class EmptyPlaylist:
        stdout = iter([])
        def wait(self): return 0
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **kw: EmptyPlaylist())
    server.run_job(isolated_job["id"])
    persisted = json.loads(server.JOBS_STATE_FILE.read_text())["jobs"][0]
    restored = server.normalize_persisted_job(persisted)
    assert restored["failure_code"] == "no_playlist_entries"
    assert "expected 12" in restored["failure_reason"]
    from retry_queue import build_retry
    new = build_retry(restored, "new-attempt")
    assert not new.get("failure_reason")
    assert not new.get("download_items_seen")



def test_log_exception_kills_downloader_process_before_releasing_gateway(
    isolated_job, monkeypatch,
):
    events = []
    monkeypatch.setattr(server, "direct_if_enabled", lambda: (
        events.append("direct") or {"direct": True}
    ))
    class Process:
        stdout = iter(["Starting extraction\n"])
        def poll(self): return None
        def terminate(self): events.append("terminate")
        def kill(self): events.append("kill")
        def wait(self, timeout=None):
            events.append("reap")
            return -15
    def spawn(command, **kwargs):
        assert kwargs["start_new_session"] is True
        events.append("spawn")
        return Process()
    monkeypatch.setattr(server.subprocess, "Popen", spawn)
    monkeypatch.setattr(server, "append_log",
                        lambda *a: (_ for _ in ()).throw(ValueError("unexpected log error")))
    server.run_job(isolated_job["id"])
    assert isolated_job["status"] == "failed"
    assert events.index("terminate") < events.index("direct", 1)
    assert events.count("direct") == 2
