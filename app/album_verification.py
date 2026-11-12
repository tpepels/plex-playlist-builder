"""Durable end-to-end downloaded-album verification.

A successful downloader job or accepted Plex scan is never marked indexed until
the actual published files appear in the fresh Plex Music media index. Checks
run independently of browser sessions and retain failure evidence across
restarts. Automatic scan retries are bounded; no audio files are modified.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

from . import download_audit, download_plex_sync
from .plex_client import connect

STATE_FILE = Path(os.getenv("ALBUM_VERIFICATION_STATE_FILE", "/data/album-verification.json"))
RECHECK_SECONDS = 3 * 60
RETRY_SECONDS = 10 * 60
MAX_RESCAN_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _lock():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(str(STATE_FILE) + ".lock", "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version":1,"last_checked":None,"items":{}, "counts":{}}
    if (not isinstance(data, dict) or data.get("version") != 1
        or not isinstance(data.get("items"),dict)
        or any(not isinstance(v,dict) for v in data["items"].values())):
        raise RuntimeError("Album verification state damaged; refusing to overwrite it")
    return data


def _write(data):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".album-verify-", dir=STATE_FILE.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as output:
            json.dump(data,output,ensure_ascii=False,separators=(",",":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(name,STATE_FILE)
        parent=os.open(STATE_FILE.parent,os.O_RDONLY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _job_key(job):
    return str(job.get("request_id") or job.get("id") or "")


def _stage(report: dict, job: dict) -> tuple[str,str]:
    outcome = report["outcome"]
    if outcome == "indexed":
        if job.get("status") == "partial":
            return "partially_downloaded", "Published tracks indexed but source download was incomplete"
        return "indexed", "Every recorded published audio file is indexed in Plex"
    if outcome == "in_progress":
        return "downloading", report["reason"]
    if outcome == "metadata_required":
        return "metadata_required", report["reason"]
    if outcome in ("missing_on_disk","partially_missing_on_disk"):
        return "missing_on_disk", report["reason"]
    if outcome in ("missing_from_plex","partially_indexed"):
        return "awaiting_index", report["reason"]
    if outcome == "unverifiable_paths":
        return "unverifiable", report["reason"]
    return "not_published", report["reason"]


def refresh(*, now=None) -> dict:
    """Reconcile all retained publications and request bounded scans if needed.

    Never assumes an accepted scan completed. The first run checks historical
    jobs too, addressing restarts/downtime and the downloader sync baseline.
    """
    now = time.time() if now is None else float(now)
    jobs = download_audit.read_downloader_jobs()
    disk = download_audit.read_disk_audit()
    _, music = connect()
    tracks = music.searchTracks()  # Actual Plex media paths, not album tags.
    reconciled = download_audit.reconcile(jobs,tracks,disk=disk)
    reports = {str(r["job_id"]):r for r in reconciled["items"]}
    with _lock():
        data=_read()
        old_items=data["items"]
        next_items={}
        for job in jobs:
            if not isinstance(job,dict) or not job.get("id"):
                continue
            job_id=str(job["id"])
            report=reports.get(job_id)
            if report is None:
                continue
            previous=old_items.get(job_id,{})
            stage,reason=_stage(report,job)
            item={
                "job_id":job_id,
                "album":report.get("requested") or report.get("title") or
                        job.get("metadata_album") or job.get("source_title") or "",
                "artist":report.get("artist") or job.get("metadata_artist") or "",
                "final_dir":report.get("final_dir") or "",
                "download_status":job.get("status") or "unknown",
                "stage":stage,"reason":reason,
                "published_files":report.get("files_recorded",0),
                "indexed_files":report.get("files_indexed",0),
                "disk_files":report.get("files_on_disk"),
                "attempts":int(previous.get("attempts",0)),
                "last_scan_request":previous.get("last_scan_request"),
                "first_observed":previous.get("first_observed") or _now(),
                "indexed_at":previous.get("indexed_at"),
                "checked_at":_now(),
            }
            if stage == "indexed":
                item["indexed_at"]=item["indexed_at"] or _now()
            elif stage == "awaiting_index":
                last=float(item["last_scan_request"] or 0)
                # Also respect another service's recent scan, avoiding duplicate
                # requests while Plex is already processing it.
                bridge=download_plex_sync.snapshot()
                most_recent=max(last,float(bridge.get("last_scan") or 0))
                if item["attempts"] >= MAX_RESCAN_ATTEMPTS:
                    item["stage"]="requires_attention"
                    item["reason"]="Plex still does not index all published files after bounded scan retries. Check library mounts, supported formats and permissions."
                elif now-most_recent >= RETRY_SECONDS:
                    # Commit the intention *before* the external call; a crash
                    # after Plex accepts it must not create a scan storm.
                    item["attempts"]+=1
                    item["last_scan_request"]=now
                    item["stage"]="scan_requested"
                    next_items[job_id]=item
                    data["items"]={**old_items,**next_items}
                    _write(data)
                    try:
                        music.update()
                        item["reason"]="Plex accepted a new scan request; indexing remains unverified"
                    except Exception as exc:
                        item["stage"]="scan_failed"
                        item["reason"]="Plex scan request failed: "+str(exc)[:200]
                elif last:
                    item["stage"]="scan_requested"
                    item["reason"]="Waiting to verify indexing after Plex scan"
            next_items[job_id]=item
        data["items"]=next_items
        data["last_checked"]=_now()
        data["counts"]=dict(Counter(x["stage"] for x in next_items.values()))
        _write(data)
        return {"last_checked":data["last_checked"],"counts":data["counts"],
                "items":list(next_items.values()),"total":len(next_items)}


def snapshot() -> dict:
    """Cheap, read-only status, safe for frequent frontend polling."""
    try:
        data=_read()
    except (OSError,ValueError,RuntimeError) as exc:
        return {"ok":False,"error":str(exc),"items":[],"counts":{},"last_checked":None}
    return {"ok":True,"last_checked":data["last_checked"],
            "counts":data.get("counts",{}),"items":list(data["items"].values()),
            "total":len(data["items"])}


async def loop():
    import asyncio
    while True:
        await asyncio.sleep(RECHECK_SECONDS)
        try:
            await asyncio.to_thread(refresh)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Persistent last-good snapshot remains available; next run retries.
            pass
