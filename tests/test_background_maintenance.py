"""Automatic cache maintenance must be read-only, bounded and resilient."""
import asyncio
from types import SimpleNamespace

from app import background_maintenance as jobs
from app import main


def test_status_api_explains_what_is_automated(monkeypatch):
    monkeypatch.setenv("WORKSPACE_AUTO_REFRESH_ENABLED", "1")
    result = main.workspace_maintenance()
    assert result["enabled"] is True
    assert result["policy"]["plex_playlist_writes"] is False
    assert result["policy"]["audio_seconds"] > 0
    assert set(result["jobs"]) == {"audio", "library"}


def test_library_job_only_reads_music_and_updates_local_index(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs.plex_client, "connect",
                        lambda: (object(), "music"))
    monkeypatch.setattr(jobs.curation_library, "get_library",
                        lambda music: calls.append(music) or {"1": {}})
    result = jobs._refresh_library()
    assert result == {"tracks": 1, "pending": 0}
    assert calls == ["music"]


def test_success_failure_and_recovery_keep_status_visible():
    async def exercise():
        name = "audio"
        result = await jobs._run_once(name, lambda: {"pending": 300})
        assert result["pending"] == 300
        assert jobs.snapshot()["jobs"][name]["pending"] == 300

        def unavailable():
            raise RuntimeError("upstream not available")

        assert await jobs._run_once(name, unavailable) is None
        failed = jobs.snapshot()["jobs"][name]
        assert failed["error"] == "upstream not available"
        assert not failed["running"]
        assert failed["pending"] == 300

        result = await jobs._run_once(name, lambda: {"pending": 0})
        assert result["pending"] == 0
        state = jobs.snapshot()["jobs"][name]
        assert state["error"] is None
        assert state["last_success"] is not None
        assert state["pending"] == 0

    asyncio.run(exercise())


def test_workers_can_be_disabled_and_shutdown_without_running_jobs(monkeypatch):
    async def exercise():
        app = SimpleNamespace(state=SimpleNamespace())
        monkeypatch.setenv("WORKSPACE_AUTO_REFRESH_ENABLED", "0")
        jobs.start_tasks(app)
        assert app.state.workspace_maintenance_tasks == []
        await jobs.stop_tasks(app)
        monkeypatch.setenv("WORKSPACE_AUTO_REFRESH_ENABLED", "1")
        jobs.start_tasks(app)
        assert len(app.state.workspace_maintenance_tasks) == 2
        await jobs.stop_tasks(app)
        assert app.state.workspace_maintenance_tasks == []

    asyncio.run(exercise())
