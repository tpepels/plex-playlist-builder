"""Idempotent Plex library scans after yt-dlp publishes a complete release.

The downloader's publication is authoritative. This module never changes
playlist definitions, downloads, media files or its queue. Newly published
jobs trigger a Plex Music rescan, and then the owner can review/add tracks
through Playlist Intelligence with explicit approval.

A durable previous-generation state prevents a reboot from repeatedly
scanning already processed albums. The first observation baselines historical
downloads; only subsequent successful publications trigger scans.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from . import downloader_bridge
from .plex_client import connect

POLL_SECONDS = 30
RETRY_SECONDS = 90
STATE_FILE = Path(os.getenv(
    "DOWNLOAD_BRIDGE_STATE_FILE", "/data/downloader-bridge.json"
))
_sync_error = ""
last_snapshot = {}


def _validate(data):
    return (isinstance(data, dict)
            and isinstance(data.get("seen"), list)
            and isinstance(data.get("pending"), list)
            and all(isinstance(value, str) and len(value) <= 160
                    for value in data["seen"] + data["pending"]))


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_valid(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not _validate(value):
        raise ValueError("Invalid scan-state schema")
    return value


def _write_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".downloader-bridge-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load(path=STATE_FILE):
    path = Path(path)
    backup = path.with_name(path.name + ".bak")
    if not path.exists() and not backup.exists():
        return None
    if path.exists():
        try:
            return _read_valid(path)
        except (OSError, ValueError, UnicodeError):
            pass

    if backup.exists():
        try:
            recovered = _read_valid(backup)
        except (OSError, ValueError, UnicodeError):
            pass
        else:
            if path.exists():
                # Quarantine a damaged primary. Keep the backup as a separate
                # valid generation for a second interrupted recovery.
                import uuid
                os.replace(path, path.with_name(
                    path.name + ".corrupt-" + uuid.uuid4().hex[:12]))
                _sync_dir(path.parent)
            _write_atomic(path, recovered)
            return recovered

    raise RuntimeError(
        "Plex download-sync state is damaged with no valid backup. "
        "Preserve downloader-bridge.json and .bak; restore a known-good copy."
    )


def _save(data, path=STATE_FILE):
    path = Path(path)
    if not _validate(data):
        raise ValueError("Invalid Plex download-sync state")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            _read_valid(path)
        except (OSError, ValueError, UnicodeError):
            raise RuntimeError(
                "Damaged Plex sync state; refusing to replace the last copy"
            ) from None
        # Preserve the previous *validated* generation by atomic hard link
        # swap. The old backup is not overwritten with corrupt bytes.
        import uuid
        backup = path.with_name(path.name + ".bak")
        temp_link = path.with_name(
            "." + path.name + ".backup-" + uuid.uuid4().hex)
        try:
            os.link(path, temp_link)
            os.replace(temp_link, backup)
            _sync_dir(path.parent)
        finally:
            temp_link.unlink(missing_ok=True)
    _write_atomic(path, data)


def published_ids(jobs):
    result = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if (job.get("status") in {"succeeded", "partial"}
                and (job.get("final_dir") or job.get("files"))):
            key = job.get("request_id") or job.get("id")
            if isinstance(key, str) and 0 < len(key) <= 160:
                result.add(key)
    return result


def scan_plex_library():
    """A scan is requested, not assumed complete or added to playlists."""
    _, music = connect()
    music.update()
    from .music_snapshot import invalidate
    invalidate()


async def poll_once(*, state_file=None):
    """Detect new published job IDs and request one Plex scan per batch.

    The pending set is committed *before* a scan. If the scanner crashes or
    Plex rejects a scan, pending IDs survive and are retried on next poll.
    """
    global _sync_error, last_snapshot
    path = Path(state_file) if state_file else STATE_FILE
    response = await downloader_bridge.upstream_json("/api/jobs", timeout=9)
    jobs = response.get("jobs", []) if isinstance(response, dict) else response
    if not isinstance(jobs, list):
        raise RuntimeError("Downloader returned invalid published-job data")
    published = published_ids(jobs)
    state = _load(path)
    if state is None:
        state = {"seen": sorted(published), "pending": [], "last_scan": None}
        _save(state, path)
        _sync_error = ""
        last_snapshot = {"pending": 0, "last_scan": None, "error": "",
                         "baseline": True}
        return last_snapshot

    seen = set(state["seen"])
    pending = set(state["pending"])
    pending.update(published - seen)
    if pending != set(state["pending"]):
        state["pending"] = sorted(pending)
        _save(state, path)

    if pending:
        try:
            await asyncio.to_thread(scan_plex_library)
        except Exception as exc:
            # Never mark the batch as scanned after an exception.
            _sync_error = "Plex library scan request failed; will retry"
            last_snapshot = {"pending": len(pending),
                             "last_scan": state.get("last_scan"),
                             "error": _sync_error}
            return last_snapshot
        state["seen"] = sorted(seen | pending)
        state["pending"] = []
        state["last_scan"] = time.time()
        _save(state, path)
        # Plex accepted a scan request. Ask the existing verifier to check
        # promptly and enqueue analysis only after file indexing is proven.
        from . import album_verification
        album_verification.request_check()

    _sync_error = ""
    last_snapshot = {"pending": 0, "last_scan": state.get("last_scan"),
                     "error": ""}
    return last_snapshot


def snapshot():
    """Read-only UI diagnostics; no network requests and no Plex scans."""
    try:
        state = _load(STATE_FILE)
    except RuntimeError as exc:
        return {"pending": None, "last_scan": None, "error": str(exc)}
    if state is None:
        return {"pending": 0, "last_scan": None,
                "error": "Awaiting first downloader observation"}
    return {"pending": len(state["pending"]),
            "last_scan": state.get("last_scan"), "error": _sync_error}


async def sync_loop():
    """Runs independently of browser sessions; bounded requests and retries."""
    while True:
        try:
            await poll_once()
        except asyncio.CancelledError:
            raise
        except (RuntimeError, OSError, ValueError) as exc:
            global _sync_error
            _sync_error = str(exc)
        await asyncio.sleep(POLL_SECONDS)
