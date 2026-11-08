"""Source-aware, deterministic bulk retry selection and job construction.

Only the most recent failed/partial attempt per source can be retried.
Existing success, in-flight work, removed history and repeated attempts are
excluded. This module performs no I/O and does not manipulate the queue.
"""
from __future__ import annotations

import time
from collections import Counter
from urllib.parse import parse_qs, urlparse

from album_submission import canonical_album_id


ACTIVE = frozenset({"queued", "running", "finalizing", "needs_metadata"})
FAILED = frozenset({"failed", "partial"})


def source_key(job):
    url = str(job.get("url") or "").strip()
    if not url:
        return None
    canonical = canonical_album_id(url)
    if canonical:
        if canonical.startswith("playlist:"):
            return ("youtube-playlist", canonical.split(":", 1)[1])
        return ("yt-album", canonical)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"music.youtube.com", "www.youtube.com", "youtube.com",
                "m.youtube.com"}:
        playlist = parse_qs(parsed.query).get("list", [])
        if playlist and playlist[0]:
            return ("youtube-playlist", playlist[0])
        video = parse_qs(parsed.query).get("v", [])
        if video and video[0]:
            return ("youtube-video", video[0])
    if host == "youtu.be":
        return ("youtube-video", parsed.path.strip("/"))
    if host == "open.spotify.com":
        return ("spotify", parsed.path.rstrip("/"))
    # Ignore URL fragment and tracking query when a canonical source identity
    # is unknown; keep full path to avoid merging different recordings.
    return ("source", host, parsed.path, parsed.query)


def _newest(job):
    return (float(job.get("created_at") or 0),
            int(job.get("attempt") or 1),
            str(job.get("id") or ""))


def failed_candidates(current, archived=None):
    """Return latest failed retries, with counts for excluded source groups."""
    archived = archived or {}
    groups = {}
    for job in list(current.values()) + list(archived.values()):
        key = source_key(job)
        if key:
            groups.setdefault(key, []).append(job)

    candidates = []
    exclusions = Counter()
    for group in groups.values():
        # A completed job can be archived, or may be older than a later
        # unnecessary failed resubmission; never bulk retry that source.
        if any(item.get("status") == "succeeded" for item in group):
            exclusions["already_succeeded"] += 1
            continue
        # Only live jobs can block as active; removed history is inert.
        if any(item.get("status") in ACTIVE for item in group
               if item.get("id") in current):
            exclusions["already_active"] += 1
            continue
        latest = max(group, key=_newest)
        if latest.get("status") not in FAILED:
            exclusions["not_failed"] += 1
            continue
        if latest.get("id") not in current:
            exclusions["removed_from_queue"] += 1
            continue
        # A published partial might have been manually completed already.
        # Do not blindly download it again in a bulk operation.
        if latest.get("final_dir"):
            exclusions["already_published"] += 1
            continue
        if latest.get("force"):
            exclusions["forced_attempt"] += 1
            continue
        candidates.append(latest)
    # Largest number of already downloaded tracks first; preserves progress
    # of near-complete albums and tends to finish them with fewer requests.
    candidates.sort(key=lambda job: (
        -int(job.get("completed_items") or 0),
        float(job.get("created_at") or 0),
        str(job.get("id") or ""),
    ))
    return candidates, dict(exclusions)


def build_retry(previous, retry_id, *, created_at=None, software_version=None):
    """Build a retry. Global ancestry and per-release attempt counts coexist."""
    job_id = previous["id"]
    software_version = str(software_version or previous.get("software_version") or "legacy")
    version_attempt = (
        int(previous.get("version_attempt") or 1) + 1
        if software_version == (previous.get("software_version") or "legacy")
        else 1
    )
    total = int(previous.get("total_items") or 0)
    completed = min(int(previous.get("completed_items") or 0), total) if total else int(previous.get("completed_items") or 0)
    source = previous.get("source", "yt-dlp")
    unresolved_urls = [
        item.get("spotify_url")
        for item in previous.get("missing_tracks", [])
        if item.get("spotify_url") and not item.get("resolved")
    ]
    download_query = (
        list(dict.fromkeys(unresolved_urls))
        if source == "spotify" and unresolved_urls
        and not previous.get("unidentified_failed_items") else None
    )
    retry_count = len(download_query or [])
    return {
        "id": retry_id,
        "url": previous["url"],
        "playlist": previous["playlist"],
        "collection_mode": previous.get("collection_mode", previous["playlist"]),
        "album_mode": previous["album_mode"],
        "compilation": previous["compilation"],
        "force": False,
        "status": "queued",
        "message": (
            f"Retrying {retry_count} failed Spotify track(s)" if retry_count
            else (f"Continuing after {completed}/{total} tracks" if total
                  else "Retry queued")
        ),
        "progress": "", "current_item": completed, "total_items": total,
        "completed_items": completed, "files": list(previous.get("files") or []),
        "log": [
            f"Retrying {retry_count} unresolved Spotify track(s) from job {job_id}."
            if retry_count else
            f"Retrying job {job_id}; existing files and archive entries will be skipped."
        ],
        "created_at": time.time() if created_at is None else created_at,
        "started_at": None, "finished_at": None, "returncode": None,
        "http_403": False, "automatic_403_restarts": 0,
        "recovered_http_403": False,
        "retry_of": job_id, "attempt": int(previous.get("attempt") or 1) + 1,
        "software_version": software_version, "version_attempt": version_attempt,
        "source": source, "retry_base_completed": completed,
        "failed_items": 0, "unidentified_failed_items": 0, "missing_tracks": [],
        "resolution_of": None, "spotify_track_url": None,
        "manual_source_url": None, "download_query": download_query,
        "staging_owner_id": previous.get("staging_owner_id") or job_id,
        "source_title": previous.get("source_title"),
        "source_album_artist": previous.get("source_album_artist"),
        "source_thumbnail": previous.get("source_thumbnail"),
        "metadata_artist": previous.get("metadata_artist"),
        "metadata_album": previous.get("metadata_album"),
        "album_submission_key": previous.get("album_submission_key"),
        "match_audit": previous.get("match_audit") or {},
        "staged_files": 0, "final_dir": previous.get("final_dir"),
        "finalize_target_id": None,
    }
