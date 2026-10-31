"""Quick offline checks: incomplete albums never reach the Plex import path."""
from album_completeness import album_hold_reason, is_album_job
from retry_queue import build_retry


def album(**overrides):
    data = {
        "id": "album", "source": "yt-dlp", "album_mode": True,
        "url": "https://music.youtube.com/playlist?list=OLAK5uy_test",
        "status": "succeeded", "returncode": 0, "total_items": 6,
        "failed_items": 0, "unidentified_failed_items": 0,
        "missing_tracks": [],
    }
    data.update(overrides)
    return data


def test_full_album_may_publish_and_unknown_count_is_held():
    assert is_album_job(album())
    assert album_hold_reason(album(), 6) is None
    assert "count" in album_hold_reason(album(total_items=0), 6)


def test_missing_or_failed_tracks_block_even_when_returncode_is_zero():
    assert "5/6" in album_hold_reason(album(), 5)
    assert "7/6" in album_hold_reason(album(), 7)
    assert album_hold_reason(album(failed_items=1), 6)
    assert album_hold_reason(album(missing_tracks=[{"ignored": True}]), 6)
    assert album_hold_reason(album(status="partial"), 6)
    assert album_hold_reason(album(returncode=1), 6)


def test_spotify_album_is_guarded_even_without_youtube_album_mode():
    spotify = album(source="spotify", album_mode=False,
                    url="https://open.spotify.com/album/1example")
    assert is_album_job(spotify)
    assert album_hold_reason(spotify, 5)
    assert album_hold_reason(spotify, 6) is None
    assert not is_album_job(album(album_mode=False, url="https://example.com/watch"))


def test_finalize_never_calls_publish_when_album_is_incomplete(tmp_path, monkeypatch):
    import app as service

    stage = tmp_path / "staging" / "album"
    stage.mkdir(parents=True)
    for number in (1, 2, 3):
        (stage / f"{number}.opus").write_bytes(b"staged")
    job = {
        **album(total_items=4),
        "staging_owner_id": "album",
        "message": "Downloaded",
        "source_title": "Four-track album",
    }
    monkeypatch.setattr(service, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(service, "persist_jobs_locked", lambda: True)
    def forbid_publish(*args, **kwargs):
        raise AssertionError("Incomplete album was published")
    monkeypatch.setattr(service.library_staging, "publish_stage", forbid_publish)
    with service.jobs_lock:
        prior = service.jobs.get("album")
        service.jobs["album"] = job
    try:
        result = service.finalize_staged_job("album")
        assert result["status"] == "incomplete"
        assert service.jobs["album"]["status"] == "partial"
        assert not service.jobs["album"].get("final_dir")
        assert len(list(stage.glob("*.opus"))) == 3
    finally:
        with service.jobs_lock:
            if prior is None:
                service.jobs.pop("album", None)
            else:
                service.jobs["album"] = prior


def test_retries_reset_version_counter_but_preserve_global_lineage():
    base = {
        "id": "root", "url": "https://music.youtube.com/playlist?list=x",
        "playlist": True, "album_mode": True, "compilation": False,
        "status": "failed", "attempt": 5, "software_version": "old",
        "version_attempt": 5, "completed_items": 3, "total_items": 4,
    }
    upgraded = build_retry(base, "new", software_version="new")
    assert upgraded["attempt"] == 6
    assert upgraded["software_version"] == "new"
    assert upgraded["version_attempt"] == 1
    same_version = build_retry(upgraded, "newer", software_version="new")
    assert same_version["attempt"] == 7
    assert same_version["version_attempt"] == 2
    assert same_version["staging_owner_id"] == "root"
