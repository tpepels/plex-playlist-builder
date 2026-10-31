"""Fast, deterministic tests for what the download dashboard tells users."""
from job_diagnostics import explain_job


def job(status, **extra):
    return {
        "status": status, "completed_items": 0, "total_items": 0,
        "archive_skipped_items": 0, "failed_items": 0, "log": [],
        "files": [], "message": "", "final_dir": None,
        **extra,
    }


def test_running_without_tracks_is_not_reported_as_downloaded():
    info = explain_job(job("running", log=[
        "WARNING: [youtube:tab] YouTube Music is not directly supported. Redirecting to YouTube"
    ]))
    assert info["heading"] == "Reading source playlist"
    assert "no individual tracks" in info["explanation"]
    assert "normal" in info["explanation"]
    assert info["tone"] == "neutral"


def test_queued_and_track_progress_are_distinct():
    assert explain_job(job("queued"))["heading"] == "Waiting in download queue"
    result = explain_job(job("running", total_items=19, completed_items=4))
    assert result["heading"] == "Downloading tracks"
    assert "4 track(s) saved out of 19" in result["explanation"]


def test_archive_skips_not_confused_with_duplicate_album_request():
    archived = explain_job(job("succeeded", archive_skipped_items=7))
    assert archived["heading"] == "Nothing new: tracks already recorded in archive"
    assert archived["duplicate_requests"] == 0
    duplicate = explain_job(job("running"), repeated_requests=2)
    assert duplicate["duplicate_requests"] == 2
    assert duplicate["archived_items"] == 0


def test_succeeded_without_files_cannot_claim_published():
    outcome = explain_job(job("succeeded"))
    assert outcome["heading"] == "Finished without new files"
    assert outcome["has_published_directory"] is False
    assert outcome["tone"] == "warning"


def test_published_album_only_when_directory_confirmed():
    outcome = explain_job(job("succeeded", final_dir="/music/Artist/Album", files=["song.opus"]))
    assert outcome["heading"] == "Album published"
    assert outcome["tone"] == "success"


def test_metadata_failure_and_partial_have_actionable_explanations():
    metadata = explain_job(job("needs_metadata"))
    assert "Move to library" in metadata["next_action"]
    failed = explain_job(job("failed", message="yt-dlp exited with code 1",
                             log=["ERROR: [youtube] Unable to download API page"]))
    assert "Unable to download API page" in failed["explanation"]
    assert failed["tone"] == "error"
    partial = explain_job(job("partial", failed_items=2))
    assert "2 item(s) failed" in partial["explanation"]
