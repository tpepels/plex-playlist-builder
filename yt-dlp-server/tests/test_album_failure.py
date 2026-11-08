"""Regression coverage for empty/archived playlists and false successes."""
from pathlib import Path

import pytest
from album_failure import diagnose_incomplete_album, safe_error
from job_history import _reason


def album(**kwargs):
    return {
        "url": "https://music.youtube.com/playlist?list=OLAK5uy_example",
        "status": "failed",
        "returncode": 0, "total_items": 12,
        "download_items_seen": 0, "archive_skipped_items": 0,
        "failure_kind": None, "last_download_error": "",
        "last_source_warning": "", **kwargs,
    }


def test_zero_of_twelve_after_clean_exit_reports_empty_enumeration():
    result = diagnose_incomplete_album(album(), 0)
    assert result["failure_code"] == "no_playlist_entries"
    assert "expected 12" in result["failure_reason"]
    assert "exit code 0" in result["failure_evidence"]
    assert "0/12 staged" in result["failure_evidence"]


def test_archive_without_staging_distinct_from_no_playlist_entries():
    result = diagnose_incomplete_album(album(archive_skipped_items=12), 0)
    assert result["failure_code"] == "archive_without_staging"
    assert "global download archive" in result["failure_reason"]
    assert "music library" in result["failure_action"]


def test_real_extractor_failure_overrides_generic_album_gate():
    result = diagnose_incomplete_album(album(returncode=1,
        last_download_error="ERROR: Playlist does not exist"), 0)
    assert result["failure_code"] == "extractor_error"
    assert "Playlist does not exist" in result["failure_reason"]


def test_bot_challenge_and_403_take_priority_over_archive():
    assert diagnose_incomplete_album(album(
        failure_kind="bot_challenge", archive_skipped_items=12), 0
    )["failure_code"] == "youtube_bot_challenge"
    assert diagnose_incomplete_album(album(
        failure_kind="http_403", returncode=1), 0
    )["failure_code"] == "youtube_http_403"


def test_nonzero_without_error_is_not_reported_as_empty_playlist():
    result = diagnose_incomplete_album(album(returncode=7), 0)
    assert result["failure_code"] == "extractor_failed_silently"
    assert "code 7" in result["failure_reason"]


def test_reached_items_with_no_audio_is_distinct():
    result = diagnose_incomplete_album(album(download_items_seen=12), 0)
    assert result["failure_code"] == "no_downloaded_audio"


def test_redirect_warning_alone_is_not_an_error():
    result = diagnose_incomplete_album(album(last_source_warning=
        "WARNING: YouTube Music is not directly supported. Redirecting"), 0)
    assert result["failure_code"] == "no_playlist_entries"


def test_hidden_or_unavailable_video_warning_is_reported():
    result = diagnose_incomplete_album(album(last_source_warning=
        "WARNING: YouTube said 12 unavailable videos are hidden"), 0)
    assert "hidden or unavailable" in result["failure_reason"]


def test_history_prefers_persisted_cause():
    cause = "Download enumerated zero playlist entries"
    assert _reason({"status":"failed", "failure_reason":cause,
                    "log":["WARNING: redirect"], "message":"0/12"}) == cause


def test_sensitive_signed_url_is_removed_from_summary():
    text = safe_error("ERROR: https://example.com/video?id=1&token=secret")
    assert "secret" not in text
    assert "https://" not in text
    assert "[source URL]" in text


def test_real_download_explicitly_disables_simulation_and_quiet():
    app = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    fn = app.split("def build_command(job, *, player_client=None):",1)[1].split("\ndef append_log(",1)[0]
    assert '"--no-simulate"' in fn
    assert '"--no-quiet"' in fn
    assert '"after_move:__YTDLP_FILE__=' in fn


def test_no_archive_mutation_in_diagnostics():
    source = (Path(__file__).resolve().parents[1] / "album_failure.py").read_text()
    assert "subprocess" not in source
    assert "unlink(" not in source
