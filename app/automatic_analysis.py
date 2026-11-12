"""Durable, serialized automatic AudioMuse analysis of music newly indexed in Plex.

The first complete Plex catalogue establishes a baseline. Subsequent newly
seen track IDs enqueue their albums exactly once, including new tracks added
to an existing album. Only the existing AudioMuse analysis API is called.
Never publish, change or delete a Plex playlist or YAML file.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from . import audiomuse_analysis, audiomuse_client

STATE_FILE = Path(os.getenv("WORKSPACE_ANALYSIS_STATE_FILE", "/data/workspace-analysis.json"))
MAX_ALBUMS = 995
COMPLETE = {"completed", "complete", "success", "succeeded", "finished", "done"}
FAILED = {"failed", "failure", "error", "cancelled", "canceled"}
MAX_RETRIES = 3


def enabled() -> bool:
    return os.getenv("WORKSPACE_AUTO_ANALYSIS_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _initial() -> dict:
    return {"version": 1, "known_tracks": [], "pending_albums": [],
            "inflight": None, "last_completed": None, "error": None,
            "attempts": {}, "album_tracks": {}, "incomplete_albums": []}


@contextmanager
def _serialized():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_FILE.with_suffix(STATE_FILE.suffix + ".lock")
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read() -> dict | None:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(state, dict) or state.get("version") != 1
        or not isinstance(state.get("known_tracks"), list)
        or not isinstance(state.get("pending_albums"), list)
        or not isinstance(state.get("attempts"), dict)
        or any(not isinstance(k, str) for k in state["known_tracks"] + state["pending_albums"])):
        raise RuntimeError("Automatic analysis state is invalid; refusing to re-enqueue music")
    return state


def _write(state: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".analysis-", dir=STATE_FILE.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(state, file, separators=(",", ":"), ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, STATE_FILE)
        directory = os.open(STATE_FILE.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def observe(library: dict) -> dict:
    """Record only tracks from a complete, successful Plex catalogue scan."""
    if not enabled():
        return {"new_tracks": 0, "new_albums": 0, "disabled": True}
    tracks = {str(k): str(row.get("album_key") or "") for k, row in library.items()
              if isinstance(row, dict) and row.get("album_key")}
    if not tracks:
        raise RuntimeError("Refusing to initialize analysis from an empty Plex catalogue")
    by_album = {}
    for key, album in tracks.items():
        by_album.setdefault(album, []).append(key)
    by_album = {key: sorted(ids) for key, ids in by_album.items()}
    with _serialized():
        state = _read()
        if state is None:
            state = _initial()
            state["known_tracks"] = sorted(tracks)
            state["album_tracks"] = by_album
            _write(state)
            return {"new_tracks": 0, "new_albums": 0, "baseline": True}
        new_tracks = set(tracks) - set(state["known_tracks"])
        added_albums = set(tracks[k] for k in new_tracks)
        inflight = set((state.get("inflight") or {}).get("albums", []))
        queued = set(state["pending_albums"])
        additions = sorted(added_albums - inflight - queued)
        state["pending_albums"].extend(additions)
        state["known_tracks"] = sorted(set(state["known_tracks"]) | set(tracks))
        if state.get("album_tracks") != by_album or new_tracks:
            state["album_tracks"] = by_album
            _write(state)
        return {"new_tracks": len(new_tracks), "new_albums": len(additions),
                "pending": len(state["pending_albums"])}


def snapshot() -> dict:
    if not enabled():
        return {"enabled": False, "pending": 0, "inflight": None, "error": None}
    # A status GET must never create /data or take a write lock. The state
    # file is published atomically and can be inspected lock-free.
    state = _read()
    if state is None:
        return {"enabled": True, "baseline": False, "pending": 0,
                "inflight": None, "error": None}
    task = state.get("inflight")
    return {"enabled": True, "baseline": True, "pending": len(state["pending_albums"]),
            "inflight": {"task_id": task["task_id"], "albums": len(task["albums"]),
                         "submitted_at": task["submitted_at"]} if task else None,
            "last_completed": state.get("last_completed"),
            "last_coverage": state.get("last_coverage"),
            "incomplete_albums": state.get("incomplete_albums", []),
            "error": state.get("error")}


def _verify_covered_albums(state: dict, albums: list[str]) -> dict:
    """Check every expected track, not merely the AudioMuse task's final state.

    The provider currently accepts only a recent-album count. Because that
    selection can shift, never claim completion until the intended exact
    Plex track IDs have both non-empty audio embeddings.
    """
    from .collaboration import _coverage
    raw_features, cache_state = audiomuse_client.cached_features()
    album_tracks = state.get("album_tracks") or {}
    missing = {}
    total = 0
    covered = 0
    for album in albums:
        keys = album_tracks.get(album) or []
        if not keys:
            missing[album] = ["catalogue-tracks-unavailable"]
            continue
        missing_ids = []
        for key in keys:
            total += 1
            status, _m, _c = _coverage(raw_features.get(key))
            if status == "complete_embeddings":
                covered += 1
            else:
                missing_ids.append(key)
        if missing_ids:
            missing[album] = missing_ids
    return {"expected_tracks": total, "covered_tracks": covered,
            "missing_albums": missing, "audio_cache_pending": cache_state.get("pending")}


def run_once() -> dict:
    """Reconcile the upstream task before submitting another bounded batch.

    Holding an OS file lock across the upstream check and POST prevents duplicate
    submissions across app workers. Pending batches are persisted across restarts.
    """
    if not enabled():
        return {"pending": 0, "disabled": True}
    with _serialized():
        state = _read()
        if state is None:
            return {"pending": 0, "baseline": False}
        if not state["pending_albums"] and not state.get("inflight"):
            return {"pending": 0}
        try:
            upstream = audiomuse_analysis.analysis_status()
        except Exception as exc:
            state["error"] = "Cannot verify AudioMuse task state: " + str(exc)[:180]
            _write(state)
            raise
        active = upstream.get("active")
        inflight = state.get("inflight")
        if inflight:
            if active:
                state["error"] = None
                _write(state)
                return {"pending": len(state["pending_albums"]), "running": True}
            last = upstream.get("last_analysis") or {}
            if last.get("task_id") != inflight["task_id"]:
                state["error"] = "Awaiting completion evidence for AudioMuse task " + inflight["task_id"]
                _write(state)
                return {"pending": len(state["pending_albums"]), "uncertain": True}
            status = str(last.get("state") or "").lower()
            if status in COMPLETE:
                # Refresh the provider's raw audio cache before making a
                # coverage decision. Bounded background sync may need more
                # than one pass to ingest all newly processed tracks.
                try:
                    synced = audiomuse_client.synchronize()
                except Exception as exc:
                    state["error"] = "AudioMuse completed but feature sync failed: " + str(exc)[:180]
                    _write(state)
                    return {"pending": len(state["pending_albums"]), "verifying": True}
                if synced.get("pending", 0):
                    state["error"] = "Waiting for AudioMuse feature cache synchronization"
                    _write(state)
                    return {"pending": len(state["pending_albums"]), "verifying": True}
                checked = _verify_covered_albums(state, inflight["albums"])
                missing = checked["missing_albums"]
                if missing:
                    completed = [a for a in inflight["albums"] if a not in missing]
                    for album in completed:
                        state["attempts"].pop(album, None)
                    exhausted = [a for a in missing if state["attempts"].get(a, 0) >= MAX_RETRIES]
                    retry = [a for a in missing if a not in exhausted]
                    state["pending_albums"] = list(dict.fromkeys(retry + state["pending_albums"]))
                    state["incomplete_albums"] = sorted(exhausted)
                    state["last_coverage"] = {
                        "task_id": inflight["task_id"], "checked_at": _now(),
                        "expected_tracks": checked["expected_tracks"],
                        "covered_tracks": checked["covered_tracks"],
                        "missing_track_ids": {
                            album: ids[:30] for album, ids in missing.items()
                        },
                        "provider_selection": "recent_album_window_not_exact_ids",
                    }
                    state["inflight"] = None
                    state["error"] = (
                        "AudioMuse task ended but " +
                        str(sum(len(ids) for ids in missing.values())) +
                        " intended tracks lack complete MusicNN/CLAP embeddings." +
                        (" Manual review required after 3 attempts." if exhausted else " Retrying.")
                    )
                    _write(state)
                    return {"pending": len(state["pending_albums"]),
                            "coverage_incomplete": len(missing),
                            "blocked": len(exhausted)}
                state["last_completed"] = {"task_id": inflight["task_id"],
                                           "albums": len(inflight["albums"]), "at": _now(),
                                           "verified_tracks": checked["covered_tracks"]}
                state["incomplete_albums"] = []
                state["last_coverage"] = {
                    "task_id": inflight["task_id"], "checked_at": _now(),
                    "expected_tracks": checked["expected_tracks"],
                    "covered_tracks": checked["covered_tracks"],
                    "missing_track_ids": {},
                }
                for album in inflight["albums"]:
                    state["attempts"].pop(album, None)
                state["inflight"] = None
                state["error"] = None
                _write(state)
            elif status in FAILED:
                retry = list(inflight["albums"])
                exhausted = [a for a in retry if state["attempts"].get(a, 0) >= MAX_RETRIES]
                if exhausted:
                    state["error"] = "AudioMuse analysis failed three times; manual review required"
                    _write(state)
                    return {"pending": len(state["pending_albums"]), "blocked": len(exhausted)}
                state["pending_albums"] = retry + state["pending_albums"]
                state["inflight"] = None
                state["error"] = "Previous AudioMuse analysis failed; retry queued"
                _write(state)
                return {"pending": len(state["pending_albums"]), "retry": True}
            else:
                return {"pending": len(state["pending_albums"]), "running": True}
        if active:
            return {"pending": len(state["pending_albums"]), "busy": True}
        pending = state["pending_albums"]
        if not pending:
            return {"pending": 0, "completed": True}
        if len(pending) > MAX_ALBUMS:
            state["error"] = "More than 995 new albums; requires a full-library analysis"
            _write(state)
            return {"pending": len(pending), "blocked": True}
        count = min(1000, len(pending) + 5)
        try:
            launched = audiomuse_analysis.start_analysis(recent_albums=count)
        except Exception as exc:
            state["error"] = "Automatic analysis submission failed: " + str(exc)[:180]
            _write(state)
            raise
        state["inflight"] = {"task_id": launched["task_id"], "albums": pending[:],
                             "submitted_at": _now()}
        state["pending_albums"] = []
        for album in pending:
            state["attempts"][album] = state["attempts"].get(album, 0) + 1
        state["error"] = None
        _write(state)
        return {"pending": 0, "started": launched["task_id"], "albums": len(pending)}
