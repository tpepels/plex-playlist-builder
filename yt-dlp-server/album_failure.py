"""Classified, durable evidence for incomplete playlist downloads.

Do not confuse the album-publication gate with the extractor failure that
caused it. Never mutate archives, media, transport or the download queue.
"""
from __future__ import annotations

import re


def safe_error(value):
    text = str(value or "").strip()
    text = re.sub(r"https?://[^\s\]<>]+", "[source URL]", text)
    text = re.sub(
        r"(?i)(?:SAPISID|SSID|SID|authorization|cookie|token)\s*[:=]\s*\S+",
        "[credential omitted]", text,
    )
    return text[:320]


def diagnose_incomplete_album(job, staged_count):
    expected = int(job.get("total_items") or 0)
    seen = int(job.get("download_items_seen") or 0)
    archive = int(job.get("archive_skipped_items") or 0)
    rc = job.get("returncode")
    error = safe_error(job.get("last_download_error"))
    warning = safe_error(job.get("last_source_warning"))
    failure = job.get("failure_kind")

    evidence = (
        f"yt-dlp exit code {rc if rc is not None else 'unknown'}; "
        f"{seen} playlist item(s) reached; {archive} archive skip(s); "
        f"{staged_count}/{expected} staged audio file(s)"
    )
    if failure == "bot_challenge":
        code = "youtube_bot_challenge"
        reason = "YouTube requested bot verification during download."
        action = "Verify your uploaded cookies and inspect the VPN recovery log before retrying."
    elif failure == "http_403" or job.get("http_403"):
        code = "youtube_http_403"
        reason = "YouTube returned HTTP 403 while yt-dlp was downloading."
        action = "Check cookies, PO-token generation and network diagnostics before retrying."
    elif error:
        code = "extractor_error"
        reason = f"yt-dlp reported: {error}"
        action = "Resolve the yt-dlp error before retrying."
    elif archive and staged_count < expected and seen <= archive:
        code = "archive_without_staging"
        reason = (
            f"yt-dlp skipped {archive} item(s) recorded in the global download "
            "archive, but the current album has no complete staged track set."
        )
        action = (
            "Check whether these tracks already exist in your music library. "
            "Only deliberately bypass the archive if you need to import them again."
        )
    elif rc is not None and int(rc) != 0:
        code = "extractor_failed_silently"
        reason = (
            f"yt-dlp exited with code {rc} without a captured error. "
            "The YouTube Music redirect warning does not explain this."
        )
        action = "Inspect the item-level log and verify the playlist and cookies."
    elif expected and seen == 0 and staged_count == 0:
        code = "no_playlist_entries"
        reason = (
            f"The source probe expected {expected} tracks, but yt-dlp "
            "enumerated none during the real download and produced no audio."
        )
        action = "Check the playlist's availability and verify the YouTube cookies."
    elif seen and staged_count == 0:
        code = "no_downloaded_audio"
        reason = f"yt-dlp reached {seen} playlist item(s) but produced no staged audio."
        action = "Inspect item-level skips, missing formats and post-processing errors."
    elif archive:
        code = "partial_archive_conflict"
        reason = (
            f"Only {staged_count}/{expected} tracks are staged, and "
            f"{archive} entries were skipped from the global download archive."
        )
        action = "Check the existing library before deciding whether to bypass the archive."
    else:
        code = "incomplete_media"
        reason = f"Only {staged_count}/{expected} expected tracks are staged."
        action = "Check the item-level log to locate missing tracks."

    if warning and code in {"no_playlist_entries", "extractor_failed_silently"}:
        if any(x in warning.lower() for x in ("unavailable", "hidden", "does not exist")):
            reason += " YouTube also warned that some content was hidden or unavailable."

    return {"failure_code": code, "failure_reason": reason,
            "failure_action": action, "failure_evidence": evidence}
