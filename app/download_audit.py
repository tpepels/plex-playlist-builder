"""Read-only reconciliation of downloader publications against Plex indexed files.

Trust file identity, not composer/performer/album string equality. A published
file path seen by Plex can have different artist and album metadata. Never
guess matches from titles alone.
"""
from __future__ import annotations

from collections import Counter
import os
from pathlib import PurePosixPath
from urllib.error import URLError
from urllib.request import Request, urlopen
import json


def _path_key(path):
    """Compare the import-relative path across differently mounted volumes."""
    if not isinstance(path, str):
        return None
    parts = PurePosixPath(path.replace("\\", "/")).parts
    # Absolute roots may differ between yt-dlp and Plex. The import root is
    # stable, while Artist/Album/Track are file-system identities, not tags.
    positions = [i for i, part in enumerate(parts)
                 if part.casefold() == "yt-dlp imports"]
    if not positions:
        return None
    tail = parts[positions[-1] + 1:]
    if len(tail) < 3 or any(p in {"", ".", ".."} for p in tail):
        return None
    # Preserve case on the Linux volume: casefolding the actual relative
    # filename would conflate two distinct media files.
    return "/".join(tail)


def _track_parts(track):
    items = getattr(track, "media", None) or []
    for media in items:
        for part in getattr(media, "parts", None) or []:
            name = getattr(part, "file", None)
            if name:
                yield name


def _indexed_files(tracks):
    index = {}
    count = 0
    for track in tracks:
        artist = str(getattr(track, "grandparentTitle", "") or "")
        album = str(getattr(track, "parentTitle", "") or "")
        title = str(getattr(track, "title", "") or "")
        for filepath in _track_parts(track):
            key = _path_key(filepath)
            if key:
                index.setdefault(key, []).append({
                    "artist": artist, "album": album, "title": title,
                    "path": filepath, "rating_key": str(getattr(track, "ratingKey", "") or ""),
                })
                count += 1
    return index, count


def _downloader_data(path):
    origin = os.getenv("YTDLP_SERVER_URL", "http://yt-dlp-server:4545").rstrip("/")
    from urllib.parse import urlparse
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
        raise ValueError("YTDLP_SERVER_URL must be an HTTP(S) server origin.")
    request = Request(origin + path, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=12) as response:
            result = json.load(response)
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"yt-dlp {path} is unavailable.") from exc
    if not isinstance(result, list):
        raise ValueError(f"yt-dlp {path} returned invalid data.")
    return result


def read_downloader_jobs():
    return _downloader_data("/api/jobs")


def read_disk_audit():
    return _downloader_data("/api/import-audit")


def _title_fields(job):
    audit = job.get("match_audit") or {}
    return [
        str(job.get("id") or ""),
        str(job.get("source_title") or ""),
        str(job.get("source_album_artist") or ""),
        str(job.get("metadata_album") or ""),
        str(job.get("metadata_artist") or ""),
        str(audit.get("requested_artist") or ""),
        str(audit.get("requested_album") or ""),
        str(audit.get("matched_artist") or ""),
        str(audit.get("matched_album") or ""),
        str(job.get("url") or ""),
        str(job.get("final_dir") or ""),
        *[str(f) for f in (job.get("files") or [])[:100]],
    ]


def reconcile(jobs, tracks, query="", disk=None):
    index, plex_file_count = _indexed_files(tracks)
    disk_by_id = {
        str(item.get("job_id")): item for item in (disk or [])
        if isinstance(item, dict) and item.get("job_id")
    }
    filter_text = str(query or "").strip().casefold()
    entries = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if filter_text and not any(filter_text in value.casefold() for value in _title_fields(job)):
            continue
        files = [file for file in (job.get("files") or []) if isinstance(file, str)]
        keys = [_path_key(file) for file in files]
        recognized = [key for key in keys if key]
        seen = [(file, index[key][0]) for file, key in zip(files, keys)
                if key and key in index]
        total = len(files)
        indexed = len(seen)
        status = str(job.get("status") or "unknown")
        published = bool(job.get("final_dir"))
        storage = disk_by_id.get(str(job.get("id")), {})
        disk_known = bool(storage)
        files_on_disk = int(storage.get("files_present") or 0) if disk_known else None
        files_missing_on_disk = int(storage.get("files_missing") or 0) if disk_known else None
        if status == "needs_metadata":
            outcome = "metadata_required"
            reason = "Downloaded audio remains staged; artist and album metadata must be approved before publication."
        elif status in {"queued", "running", "finalizing"}:
            outcome = "in_progress"
            reason = "The job has not completed library publication."
        elif published and disk_known and files_missing_on_disk and not files_on_disk:
            outcome = "missing_on_disk"
            reason = "The downloader records publication, but all recorded files are now absent from its music volume."
        elif published and disk_known and files_missing_on_disk:
            outcome = "partially_missing_on_disk"
            reason = "Some published files are missing from yt-dlp's music volume; check the file paths before rescanning Plex."
        elif published and not recognized:
            outcome = "unverifiable_paths"
            reason = "A publish destination exists, but its saved file paths cannot be compared with Plex."
        elif published and total and indexed == total:
            outcome = "indexed"
            reason = "Every published audio file in this job is indexed in Plex, regardless of displayed tags."
        elif published and indexed:
            outcome = "partially_indexed"
            reason = f"Plex indexes {indexed} of {total} recorded published files."
        elif published and total:
            outcome = "missing_from_plex"
            reason = f"No recorded published file matches a media file indexed in Plex ({total} expected)."
        elif published:
            outcome = "unverifiable_paths"
            reason = "The job has a publication directory but no recorded audio paths to verify."
        elif status in {"failed", "partial"}:
            outcome = "not_published"
            reason = "The job failed or was partial and has no confirmed publication directory."
        else:
            outcome = "no_published_files"
            reason = "No published album path is recorded. This does not establish that Plex contains the music."
        plex_labels = []
        for _, match in seen:
            label = {"artist": match["artist"], "album": match["album"]}
            if label not in plex_labels:
                plex_labels.append(label)
        entries.append({
            "job_id": str(job.get("id") or ""),
            "title": str(job.get("metadata_album") or job.get("source_title") or "Untitled source"),
            "artist": str(job.get("metadata_artist") or job.get("source_album_artist") or ""),
            "status": status,
            "outcome": outcome, "reason": reason,
            "final_dir": job.get("final_dir") or "",
            "files_recorded": total, "files_indexed": indexed,
            "unmatched_files": [file for file, key in zip(files, keys)
                                if not key or key not in index][:10],
            "plex_labels": plex_labels[:10],
            "requested": (job.get("match_audit") or {}).get("requested_album") or "",
            "source_url": str(job.get("url") or ""),
            "staged_files": int(job.get("staged_files") or 0),
            "files_on_disk": files_on_disk,
            "files_missing_on_disk": files_missing_on_disk,
            "final_dir_exists": storage.get("final_dir_exists") if disk_known else None,
            "staged_audio_on_disk": storage.get("staged_audio_count") if disk_known else None,
            "created_at": job.get("created_at"),
        })
    counts = Counter(item["outcome"] for item in entries)
    return {
        "jobs_considered": len(entries),
        "downloader_jobs_available": len(jobs),
        "plex_import_files_indexed": plex_file_count,
        "counts": dict(counts),
        "items": entries,
        "limitations": ("Only jobs retained by yt-dlp are checked. Paths are matched "
                        "under YT-DLP Imports, even if Plex mounts the folder elsewhere. "
                        "A missing Plex match can mean an unscanned file, a library mount "
                        "problem, or an indexing failure; it does not prove the files are lost."),
    }
