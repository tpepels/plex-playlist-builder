"""Fail-closed gate: an album is never imported when its track set is incomplete.

The expected count must come from a real source probe or download progress.
Counting files alone cannot prove track identity, but a count mismatch reliably
proves that the album must stay outside Plex.
"""
from urllib.parse import urlparse


def is_album_job(job):
    if job.get("album_mode"):
        return True
    if job.get("source") == "spotify":
        return urlparse(str(job.get("url") or "")).path.startswith("/album/")
    return False


def album_hold_reason(job, staged_count):
    """Return a reason to keep an album in staging, or None when safe to import."""
    if not is_album_job(job):
        return None
    failed = int(job.get("failed_items") or 0)
    if failed or int(job.get("unidentified_failed_items") or 0):
        return "download reports missing or failed tracks"
    if any(not item.get("resolved") for item in job.get("missing_tracks") or []):
        return "unresolved tracks remain (including ignored YouTube tracks)"
    if job.get("status") not in {"succeeded", "needs_metadata"}:
        return "download has not completed successfully"
    if job.get("returncode") is None or int(job["returncode"]) != 0:
        return "the downloader did not report a clean completion"
    expected = int(job.get("total_items") or 0)
    if expected <= 0:
        return "the expected track count could not be verified"
    if staged_count != expected:
        return f"{staged_count}/{expected} tracks available in staging"
    return None
