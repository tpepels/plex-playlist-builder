"""Evidence-based, human-readable download outcomes for the job dashboard.

A server acknowledgement, an archive skip and a published album are distinct.
Never infer that downloads succeeded from a running subprocess or a missing log.
"""
from __future__ import annotations


def _error_evidence(log):
    for raw in reversed(log or []):
        line = str(raw).strip()
        if line.startswith(("ERROR:", "ERROR ", "FATAL:", "Traceback")):
            return line[:350]
    return ""


def explain_job(job, repeated_requests=0):
    status = job.get("status", "unknown")
    completed = int(job.get("completed_items") or 0)
    total = int(job.get("total_items") or 0)
    archived = int(job.get("archive_skipped_items") or 0)
    failed = int(job.get("failed_items") or 0)
    files = job.get("files") or []
    log = job.get("log") or []
    error = _error_evidence(log)
    explanation = ""
    action = ""
    tone = "neutral"
    heading = "Checking status"

    if status == "running" and str(job.get("message") or "").startswith("YouTube HTTP 403; waiting"):
        heading = "Recovering from HTTP 403"
        explanation = str(job.get("message"))
        action = "Automatic retry is scheduled. Previously saved tracks are retained."
        tone = "warning"
    elif status == "queued":
        heading = "Waiting in download queue"
        explanation = "The request is accepted. yt-dlp has not started processing it yet."
        action = "No action required."
    elif status == "running":
        if completed:
            heading = "Downloading tracks"
            explanation = (f"{completed} track(s) saved"
                           + (f" out of {total}" if total else "")
                           + ". The album is not yet published to Plex.")
        elif total:
            heading = "Track list found; preparing downloads"
            explanation = (f"Found {total} playlist items, but no saved tracks have "
                           "been confirmed yet.")
        else:
            heading = "Reading source playlist"
            explanation = ("yt-dlp is running, but no individual tracks have been "
                           "confirmed yet. This does not prove a download has started.")
        if any("YouTube Music is not directly supported" in str(line) for line in log):
            explanation += (" The YouTube Music to YouTube playlist redirect "
                            "is normal.")
        action = "Wait for track progress or inspect the log."
    elif status == "finalizing":
        heading = "Importing into music library"
        explanation = "The downloader is processing staged media and metadata."
        action = "Wait for the publication result."
    elif status == "needs_metadata":
        heading = "Action needed: album metadata"
        explanation = ("Tracks are staged, but not published. Artist and album "
                       "information must be confirmed.")
        action = "Enter artist and album, then choose Move to library."
        tone = "warning"
    elif status == "succeeded":
        if job.get("final_dir"):
            heading = "Album published"
            explanation = "The album was published to the music library."
            action = "No action required."
            tone = "success"
        elif not files and completed == 0:
            if archived:
                heading = "Nothing new: tracks already recorded in archive"
                explanation = (f"yt-dlp explicitly skipped {archived} item(s) "
                               "because the download archive already records them. "
                               "No new album files were published by this job.")
            else:
                heading = "Finished without new files"
                explanation = ("yt-dlp finished, but no saved files or library "
                               "publication are confirmed. Check the log.")
            action = "Inspect the log and existing library before retrying."
            tone = "warning"
        else:
            heading = "Download finished"
            explanation = ("yt-dlp completed. No published library destination is "
                           "confirmed for this job.")
            action = "Review the final log and metadata."
            tone = "warning"
    elif status == "partial":
        heading = "Partial download"
        explanation = (f"Some tracks were processed, but {failed or 'one or more'} "
                       "item(s) failed.")
        action = "Review missing tracks or retry the failed items."
        tone = "warning"
    elif status == "failed":
        heading = "Download failed"
        explanation = error or str(job.get("message") or "The downloader failed.")
        action = "Inspect the log and use Retry / continue."
        tone = "error"
    else:
        heading = f"Download state: {status}"
        explanation = str(job.get("message") or "Status not yet available.")
        action = "Inspect the log."

    return {
        "heading": heading,
        "explanation": explanation,
        "next_action": action,
        "tone": tone,
        "archived_items": archived,
        "duplicate_requests": max(0, int(repeated_requests or 0)),
        "file_count": len(files),
        "has_published_directory": bool(job.get("final_dir")),
        "automatic_403_restarts": int(job.get("automatic_403_restarts") or 0),
        "recovered_http_403": bool(job.get("recovered_http_403")),
    }
