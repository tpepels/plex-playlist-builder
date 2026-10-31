"""Searchable, durable download-attempt summaries (including removed UI cards)."""
from __future__ import annotations


def _reason(job):
    for line in reversed(job.get("log") or []):
        value = str(line).strip()
        if value.startswith(("ERROR:", "FATAL:")) or "HTTP Error 403" in value:
            return value[:500]
    if job.get("http_403"):
        return "HTTP 403: the source server refused the video data."
    return str(job.get("message") or "")[:500]


def history_records(current, archived, *, query="", status="all", offset=0, limit=100):
    query = str(query or "").casefold().strip()
    if status not in ("all", "failed", "incomplete", "published"):
        raise ValueError("Invalid history status filter.")
    raw = [(job, False) for job in current.values()] + [
        (job, True) for job in archived.values()
    ]
    entries = []
    for job, removed in raw:
        state = str(job.get("status") or "unknown")
        if status == "failed" and state not in {"failed", "partial"}:
            continue
        if status == "incomplete" and state not in {
            "queued", "running", "finalizing", "needs_metadata", "failed", "partial"
        }:
            continue
        if status == "published" and not job.get("final_dir"):
            continue
        match = job.get("match_audit") or {}
        title = str(job.get("metadata_album") or
                    match.get("requested_album") or job.get("source_title") or "")
        artist = str(job.get("metadata_artist") or
                     match.get("requested_artist") or
                     job.get("source_album_artist") or "")
        reason = _reason(job)
        if query and query not in (" ".join((
            str(job.get("id") or ""), str(job.get("url") or ""),
            title, artist, reason, str(job.get("final_dir") or ""),
            str(match.get("matched_album") or ""),
        ))).casefold():
            continue
        entries.append({
            "job_id": str(job.get("id") or ""),
            "url": str(job.get("url") or ""),
            "title": title, "artist": artist, "status": state,
            "reason": reason, "http_403": bool(job.get("http_403") or
                                               "403" in reason),
            "removed_from_queue": removed,
            "staging_discarded": bool(job.get("staging_discarded")),
            "created_at": job.get("created_at"),
            "finished_at": job.get("finished_at"),
            "final_dir": job.get("final_dir") or "",
            "completed_items": int(job.get("completed_items") or 0),
            "total_items": int(job.get("total_items") or 0),
            "returncode": job.get("returncode"),
            "retry_of": job.get("retry_of"),
            "match_type": match.get("match_type") or "",
        })
    entries.sort(key=lambda item: (float(item["created_at"] or 0),
                                   item["job_id"]), reverse=True)
    count = len(entries)
    return {"total": count, "offset": offset, "limit": limit,
            "items": entries[offset:offset + limit],
            "complete_history_since_update": True,
            "limitation": ("History begins with still-retained records at upgrade. "
                           "Jobs already pruned/deleted by older builds cannot "
                           "be reconstructed from this database.")}
