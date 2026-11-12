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

from . import download_audit, download_plex_sync, automatic_analysis, background_maintenance
from .insights import track_model
from .plex_client import connect

STATE_FILE = Path(os.getenv("ALBUM_VERIFICATION_STATE_FILE", "/data/album-verification.json"))
RECHECK_SECONDS = 3 * 60
RETRY_SECONDS = 10 * 60
MAX_RESCAN_ATTEMPTS = 3
_LAST_ERROR = ""


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


def _indexed_download_tracks(jobs: list[dict], tracks: list) -> tuple[dict, dict]:
    """Resolve downloader files to exact Plex rating keys using the same
    normalized import-relative file identity as the disk reconciliation.

    A missing or ambiguous file identity is an error, never a best-guess
    artist/album title match. Return the complete contemporaneous Plex album
    mapping so the AudioMuse queue can verify every track in each release.
    """
    all_tracks = {}
    by_file = {}
    for track in tracks:
        identity = track_model(track)
        key, album = identity["key"], identity["album_key"]
        if not key or not album:
            continue
        all_tracks[key] = album
        for path in download_audit._track_parts(track):
            relative = download_audit._path_key(path)
            if relative:
                by_file.setdefault(relative, set()).add(key)
    downloads = {}
    for job in jobs:
        files = job.get("files") or []
        if not files:
            raise ValueError("Published job has no recorded audio paths.")
        matched = {}
        for filename in files:
            relative = download_audit._path_key(filename)
            choices = by_file.get(relative) if relative else None
            if not choices or len(choices) != 1:
                raise ValueError(
                    "Published file has no unique Plex track identity: " +
                    str(filename)[-140:])
            key = next(iter(choices))
            matched[key] = all_tracks[key]
        downloads[str(job["id"])] = matched
    return downloads, all_tracks


def refresh(*, now=None) -> dict:
    """Reconcile all retained publications and request bounded scans if needed.

    Never assumes an accepted scan completed. The first run checks historical
    jobs too, addressing restarts/downtime and the downloader sync baseline.
    """
    global _LAST_ERROR
    now = time.time() if now is None else float(now)
    jobs = download_audit.read_downloader_jobs()
    disk = download_audit.read_disk_audit()
    _, music = connect()
    tracks = music.searchTracks()  # Actual Plex media paths, not album tags.
    reconciled = download_audit.reconcile(jobs,tracks,disk=disk)
    reports = {str(r["job_id"]):r for r in reconciled["items"]}
    newly_queued = False
    with _lock():
        data=_read()
        old_items=data["items"]
        next_items={}
        scan_jobs=[]
        verified_handoffs=[]
        existing_observation = bool(data.get("last_checked"))
        bridge=download_plex_sync.snapshot()
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
                # Old indexed downloads predate this event integration. Do
                # not retroactively enqueue the entire existing collection
                # when the feature is deployed or the verifier is initialized.
                prior_handoff=previous.get("analysis_handoff")
                if prior_handoff:
                    handoff=prior_handoff
                elif previous.get("stage")=="indexed" and (
                    previous.get("published_files")==item["published_files"]):
                    handoff="historical"
                elif existing_observation:
                    handoff="pending"
                else:
                    handoff="historical"
                item["analysis_handoff"]=handoff
                if handoff=="pending":
                    verified_handoffs.append(job)
            elif stage == "awaiting_index":
                last=float(item["last_scan_request"] or 0)
                # Also respect another service's recent scan, avoiding duplicate
                # requests while Plex is already processing it.
                most_recent=max(last,float(bridge.get("last_scan") or 0))
                if item["attempts"] >= MAX_RESCAN_ATTEMPTS:
                    item["stage"]="requires_attention"
                    item["reason"]="Plex still does not index all published files after bounded scan retries. Check library mounts, supported formats and permissions."
                elif now-most_recent >= RETRY_SECONDS:
                    # Batch all eligible albums into ONE Plex scan per pass.
                    # Reserve and persist the attempt before requesting it.
                    item["attempts"]+=1
                    item["last_scan_request"]=now
                    item["stage"]="scan_requested"
                    scan_jobs.append(job_id)
                elif last:
                    item["stage"]="scan_requested"
                    item["reason"]="Waiting to verify indexing after Plex scan"
            next_items[job_id]=item
        if scan_jobs:
            data["items"]={**old_items,**next_items}
            _write(data)
            try:
                music.update()
                from .music_snapshot import invalidate
                invalidate()
                for key in scan_jobs:
                    next_items[key]["reason"] = (
                        "Plex accepted one batched scan request; indexing remains unverified")
            except Exception as exc:
                for key in scan_jobs:
                    next_items[key]["stage"]="scan_failed"
                    next_items[key]["reason"]="Plex scan request failed: "+str(exc)[:200]
        data["items"]=next_items
        data["last_checked"]=_now()
        data["counts"]=dict(Counter(x["stage"] for x in next_items.values()))
        # This first write makes every pending handoff durable. If the process
        # crashes before the analysis queue accepts it, the next verification
        # pass retries it. The analysis queue deduplicates by downloader job ID.
        _write(data)
        if verified_handoffs and background_maintenance.enabled() and automatic_analysis.enabled():
            try:
                matched, all_plex_tracks = _indexed_download_tracks(verified_handoffs, tracks)
                admission = automatic_analysis.observe_verified_downloads(
                    matched, all_plex_tracks)
                if not admission.get("disabled"):
                    acknowledged=set(admission.get("acknowledged_jobs") or [])
                    # Already accepted job IDs must also be acknowledged
                    # locally after a crash between the two durable writes.
                    acknowledged.update(matched)
                    for job_id in acknowledged:
                        next_items[job_id]["analysis_handoff"]="queued"
                        next_items[job_id].pop("analysis_error", None)
                    newly_queued=bool(admission.get("pending"))
            except Exception as exc:
                for job in verified_handoffs:
                    next_items[str(job["id"])]["analysis_error"]=str(exc)[:200]
                # Keep pending: failure cannot erase the analysis trigger.
            _write(data)
        _LAST_ERROR = ""
        result={"last_checked":data["last_checked"],"counts":data["counts"],
                "items":list(next_items.values()),"total":len(next_items)}
    # A confirmed indexed album launches the existing AudioMuse controller
    # immediately; the periodic two-minute worker still reconciles failures,
    # upstream busy state, and tracks from all other import sources.
    if newly_queued:
        try:
            result["analysis_trigger"]=automatic_analysis.run_once()
        except Exception as exc:
            result["analysis_trigger_error"]=str(exc)[:200]
    return result


def snapshot() -> dict:
    """Cheap, read-only status, safe for frequent frontend polling."""
    try:
        data=_read()
    except (OSError,ValueError,RuntimeError) as exc:
        return {"ok":False,"error":str(exc),"items":[],"counts":{},"last_checked":None}
    return {"ok":not bool(_LAST_ERROR), "error":_LAST_ERROR or None,
            "last_checked":data["last_checked"],
            "counts":data.get("counts",{}),"items":list(data["items"].values()),
            "total":len(data["items"])}


async def loop():
    import asyncio
    delay=15
    while True:
        await asyncio.sleep(delay)
        delay=RECHECK_SECONDS
        try:
            await asyncio.to_thread(refresh)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            global _LAST_ERROR
            # Surface failed background checks while preserving last-good data.
            _LAST_ERROR=str(exc)[:240]
