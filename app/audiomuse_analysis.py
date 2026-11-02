"""Limited AudioMuse analysis controls for the playlist manager.

Only the upstream analysis/start and status APIs are used.
No clustering, cleanup, playlist creation, or cancellation endpoints.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .audiomuse_client import _settings


class AudioMuseControlError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def _call(path: str, *, post: dict | None = None) -> dict:
    url, token = _settings()
    headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
    if post is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        url + path,
        headers=headers,
        data=json.dumps(post).encode("utf-8") if post is not None else None,
        method="POST" if post is not None else "GET",
    )
    try:
        with urlopen(request, timeout=12) as response:
            body = json.load(response)
    except HTTPError as exc:
        if exc.code in (400, 409, 422):
            raise AudioMuseControlError(
                "AudioMuse rejected the request, possibly because another main task is running.",
                409 if exc.code == 409 else 422,
            ) from exc
        if exc.code in (401, 403):
            raise AudioMuseControlError("AudioMuse authentication denied.", 503) from exc
        raise AudioMuseControlError(f"AudioMuse returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise AudioMuseControlError("AudioMuse control API unavailable.") from exc
    if not isinstance(body, dict):
        raise AudioMuseControlError("Unexpected AudioMuse response.")
    return body


def _text(value: object, max_length: int = 180) -> str:
    return str(value or "")[:max_length]


def _summary(task: dict) -> dict | None:
    task_id = task.get("task_id")
    if not task_id:
        return None
    details = task.get("details")
    if not isinstance(details, dict):
        details = {}
    task_type = _text(task.get("task_type") or task.get("task_type_from_db"), 50)
    raw_progress = task.get("progress") or 0
    try:
        progress = max(0, min(100, float(raw_progress)))
    except (TypeError, ValueError):
        progress = 0
    return {
        "task_id": _text(task_id, 80),
        "task_type": task_type,
        "state": _text(task.get("state") or task.get("status"), 50),
        "progress": progress,
        "message": _text(
            task.get("status_message")
            or details.get("status_message")
            or details.get("message")
            or details.get("current_album")
            or ""
        ),
    }


def analysis_status() -> dict:
    """Show the currently active main task and recent analysis, if present."""
    active = _summary(_call("/api/active_tasks"))
    last = _summary(_call("/api/last_task"))
    return {
        "active": active,
        "last_analysis": last if last and last["task_type"] == "main_analysis" else None,
        "busy": active is not None,
    }


def start_analysis(*, recent_albums: int, confirm_full_library: bool = False) -> dict:
    if not isinstance(recent_albums, int) or isinstance(recent_albums, bool):
        raise ValueError("Recent album count must be an integer.")
    if recent_albums < 0 or recent_albums > 1000:
        raise ValueError("Recent album count must be between 0 and 1000.")
    if recent_albums == 0 and not confirm_full_library:
        raise ValueError("Full-library analysis requires explicit confirmation.")
    result = _call("/api/analysis/start", post={"num_recent_albums": recent_albums})
    if not result.get("task_id"):
        raise AudioMuseControlError("AudioMuse did not return an analysis task ID.")
    return {
        "task_id": _text(result["task_id"], 80),
        "state": _text(result.get("status") or "queued"),
        "recent_albums": recent_albums,
        "full_library": recent_albums == 0,
    }
