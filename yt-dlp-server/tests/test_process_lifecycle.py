"""Inactivity and retagging failure policies without network or media tools."""
import subprocess
from pathlib import Path

import pytest

import process_lifecycle as life
import staging
from download_resilience import needs_vpn_rotation, should_restart, recovery_policy


@pytest.mark.parametrize("raw,expected", [
    ({}, 900), ({"DOWNLOAD_STALL_SECONDS":"bad"}, 900),
    ({"DOWNLOAD_STALL_SECONDS":"3"}, 120),
    ({"DOWNLOAD_STALL_SECONDS":"99999"}, 3600),
    ({"DOWNLOAD_STALL_SECONDS":"1800"}, 1800),
])
def test_stall_limit_is_safe_and_bounded(raw, expected):
    assert life.inactivity_limit(raw) == expected


def test_silent_child_is_terminated_after_monotonic_deadline(monkeypatch):
    events = []
    class Child:
        def poll(self): return None if not events else 0
        def terminate(self): events.append("term")
        def kill(self): events.append("kill")

    guard = life.InactivityGuard(Child(), seconds=30, interval=0.1)
    guard._last = 1
    monkeypatch.setattr(life.time, "monotonic", lambda: 40)
    monkeypatch.setattr(guard._stop, "wait", lambda seconds: False)
    guard._watch()
    assert guard.timed_out is True
    assert events == ["term"]


def test_unresponsive_child_escalates_to_kill(monkeypatch):
    events = []
    class Child:
        def poll(self): return None
        def terminate(self): events.append("term")
        def kill(self): events.append("kill")

    guard = life.InactivityGuard(Child(), seconds=10)
    guard._last = 1
    monkeypatch.setattr(life.time, "monotonic", lambda: 100)
    monkeypatch.setattr(guard._stop, "wait", lambda timeout: False)
    guard._watch()
    assert guard.timed_out
    assert events == ["term", "kill"]


def test_recent_progress_does_not_kill_downloader(monkeypatch):
    class Child:
        def poll(self): return None
        def terminate(self): pytest.fail("recent progress must not be aborted")
        def kill(self): pytest.fail("recent progress must not be killed")

    guard = life.InactivityGuard(Child(), seconds=30, interval=1)
    guard._last = 70
    monkeypatch.setattr(life.time, "monotonic", lambda: 80)
    count = 0
    def wait(seconds):
        nonlocal count
        count += 1
        return count >= 2
    monkeypatch.setattr(guard._stop, "wait", wait)
    guard._watch()
    assert guard.timed_out is False


def test_stalled_source_does_not_trigger_vpn_or_automatic_retry():
    policy = recovery_policy({"YOUTUBE_403_RESTARTS":"2"})
    assert not should_restart(
        youtube=True, spotify=False, forced=False, returncode=-15,
        failure_kind="stalled", already_restarted=0, policy=policy
    )
    assert not needs_vpn_rotation("stalled", 1, policy)


def test_ffmpeg_timeout_keeps_original_media_and_cleans_temp(tmp_path, monkeypatch):
    file = tmp_path / "Original [videoid00].opus"
    file.write_bytes(b"original-track")

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 120
        temporary = Path(args[0][-1])
        temporary.write_bytes(b"partial-invalid-output")
        raise subprocess.TimeoutExpired(args[0], 120)

    monkeypatch.setattr(staging.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        staging._retag_audio_only_with_ffmpeg(file, "Album", "Artist")

    assert file.read_bytes() == b"original-track"
    assert list(tmp_path.iterdir()) == [file]



def test_exception_cleanup_terminates_hung_child(monkeypatch):
    events = []
    class Child:
        def poll(self): return None
        def terminate(self): events.append("term")
        def kill(self): events.append("kill")
        def wait(self, timeout):
            assert timeout == 5
            raise subprocess.TimeoutExpired("yt-dlp", timeout)

    life.stop_process(Child())
    assert events == ["term", "kill"]


def test_abort_signals_full_process_group_not_only_parent(monkeypatch):
    import signal
    sent = []
    child = type("Child", (), {"pid": 12345})()
    monkeypatch.setattr(life.os, "killpg",
                        lambda pid, sig: sent.append((pid, sig)))
    life._signal(child, signal.SIGTERM)
    life._signal(child, signal.SIGKILL)
    assert sent == [
        (12345, signal.SIGTERM),
        (12345, signal.SIGKILL),
    ]
