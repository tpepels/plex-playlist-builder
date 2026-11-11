"""Background cache updates and new-music analysis. Never writes Plex playlists."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from typing import Any

from starlette.concurrency import run_in_threadpool

from . import audiomuse_client, automatic_analysis, curation_library, plex_client

_AUDIO_INTERVAL = 15 * 60
_LIBRARY_INTERVAL = 15 * 60
_ANALYSIS_INTERVAL = 2 * 60
_START_DELAY = 20
_state: dict[str, dict[str, Any]] = {
    "audio": {"running": False, "last_attempt": None, "last_success": None,
              "error": None, "pending": None},
    "library": {"running": False, "last_attempt": None, "last_success": None,
                "error": None, "pending": None},
    "analysis": {"running": False, "last_attempt": None, "last_success": None,
                 "error": None, "pending": None},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enabled() -> bool:
    return os.getenv("WORKSPACE_AUTO_REFRESH_ENABLED", "1").lower() not in ("0", "false", "no")


def snapshot() -> dict[str, Any]:
    return {"enabled": enabled(), "jobs": {name: dict(value) for name, value in _state.items()},
            "automatic_analysis": automatic_analysis.snapshot(),
            "policy": {"audio_seconds": _AUDIO_INTERVAL,
                       "library_seconds": _LIBRARY_INTERVAL,
                       "analysis_seconds": _ANALYSIS_INTERVAL,
                       "plex_playlist_writes": False}}


def _refresh_library() -> dict[str, Any]:
    _, music = plex_client.connect()
    # When deploying automatic analysis into an existing installation, seed its
    # first baseline from the LAST persisted catalogue before refreshing Plex.
    # This detects releases added while the manager was offline. Never force a
    # network scan to establish that baseline.
    state = automatic_analysis.snapshot()
    if state.get("enabled") and not state.get("baseline"):
        if curation_library.metadata().get("updated"):
            previous = curation_library.get_library(music)
            automatic_analysis.observe(previous)
    # A successful scheduled run must mean Plex was actually rescanned.
    tracks = curation_library.get_library(music, refresh=True)
    observed = automatic_analysis.observe(tracks)
    return {"tracks": len(tracks), "new_tracks": observed.get("new_tracks", 0),
            "new_albums": observed.get("new_albums", 0), "pending": 0}


async def _run_once(name: str, task) -> dict[str, Any] | None:
    state = _state[name]
    state.update(running=True, last_attempt=_now())
    try:
        result = await run_in_threadpool(task)
        state.update(last_success=_now(), error=None,
                     pending=int(result.get("pending", 0)))
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Preserve previously synchronized caches and surface failures; retry later.
        state["error"] = str(exc)[:300]
        return None
    finally:
        state["running"] = False


async def _loop(name: str, task, interval: int) -> None:
    delay = _START_DELAY
    while True:
        await asyncio.sleep(delay)
        result = await _run_once(name, task)
        # Continue a bounded AudioMuse sync over successive batches, but do
        # not busy-loop when the provider is down.
        delay = 5 if name == "audio" and result and result.get("pending", 0) else interval


def start_tasks(app) -> None:
    if not enabled():
        app.state.workspace_maintenance_tasks = []
        return
    app.state.workspace_maintenance_tasks = [
        asyncio.create_task(_loop("audio", audiomuse_client.synchronize,
                                  _AUDIO_INTERVAL), name="workspace-audio-sync"),
        asyncio.create_task(_loop("library", _refresh_library,
                                  _LIBRARY_INTERVAL), name="workspace-library-refresh"),
        asyncio.create_task(_loop("analysis", automatic_analysis.run_once,
                                  _ANALYSIS_INTERVAL), name="workspace-auto-analysis"),
    ]


async def stop_tasks(app) -> None:
    tasks = getattr(app.state, "workspace_maintenance_tasks", [])
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    app.state.workspace_maintenance_tasks = []
