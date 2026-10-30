"""Offline tests for the idempotent JSON album submission client."""
import io
import json
from urllib.error import HTTPError, URLError

import pytest
from app import ytdlp_submit as submit


class Accepted:
    def __init__(self, result):
        self.result = result
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def read(self):
        return json.dumps(self.result).encode("utf-8")


def test_album_submission_returns_job_id_and_audit(monkeypatch):
    calls = []
    def fake_open(request, timeout):
        calls.append((request, timeout))
        return Accepted({"ok": True, "duplicate": False, "job_id": "abc123", "status": "queued"})
    monkeypatch.setattr(submit, "urlopen", fake_open)
    monkeypatch.setenv("YTDLP_SERVER_URL", "http://yt-dlp-server:4545")
    url = "https://music.youtube.com/playlist?list=OLAK5uy_verified"
    result = submit.queue_album(url, {"match_type": "exact"})
    assert result == {"job_id": "abc123", "duplicate": False, "status": "queued"}
    request, timeout = calls[0]
    assert request.get_method() == "POST"
    assert request.full_url == "http://yt-dlp-server:4545/api/albums"
    assert json.loads(request.data) == {
        "url": url, "match_audit": {"match_type": "exact"},
    }


def test_repeated_album_returns_original_job_id(monkeypatch):
    monkeypatch.setattr(submit, "urlopen", lambda *args, **kwargs: Accepted(
        {"ok": True, "duplicate": True, "job_id": "original", "status": "succeeded"}))
    result = submit.queue_album("https://music.youtube.com/playlist?list=OLAK5uy_verified")
    assert result == {"job_id": "original", "duplicate": True, "status": "succeeded"}


@pytest.mark.parametrize("url", [
    "https://music.youtube.com/search?q=Stan+Getz",
    "https://music.youtube.com/watch?v=bad",
    "https://evil.example/playlist?list=OLAK5uy_wrong",
    "https://music.youtube.com/playlist?list=OTHER_ID",
])
def test_invalid_urls_rejected_before_http(monkeypatch, url):
    monkeypatch.setattr(submit, "urlopen", lambda *args, **kwargs: pytest.fail("HTTP called"))
    with pytest.raises(submit.AlbumQueueError, match="invalid download URL"):
        submit.queue_album(url)


def test_queue_http_error_does_not_claim_success(monkeypatch):
    def fail(request, timeout):
        raise HTTPError(request.full_url, 429, "Full", {}, io.BytesIO())
    monkeypatch.setattr(submit, "urlopen", fail)
    with pytest.raises(submit.AlbumQueueError, match="queue is full"):
        submit.queue_album("https://music.youtube.com/playlist?list=OLAK5uy_valid")


def test_network_timeout_reports_uncertainty_and_can_safely_retry(monkeypatch):
    def fail(request, timeout):
        raise URLError("timeout")
    monkeypatch.setattr(submit, "urlopen", fail)
    with pytest.raises(submit.AlbumQueueError, match="Repeating the same request is safe"):
        submit.queue_album("https://music.youtube.com/playlist?list=OLAK5uy_valid")


def test_album_browse_id_enriches_to_audio_playlist(monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, "ytmusicapi", SimpleNamespace(
        YTMusic=lambda: SimpleNamespace(
            get_album=lambda id: {"audioPlaylistId": "OLAK5uy_verified"})))
    submit._download_url.cache_clear()
    try:
        assert submit._download_url("https://music.youtube.com/browse/MPREalbum") == (
            "https://music.youtube.com/playlist?list=OLAK5uy_verified")
    finally:
        submit._download_url.cache_clear()


def test_public_url_never_uses_docker_dns(monkeypatch):
    monkeypatch.setenv("YTDLP_PUBLIC_URL", "http://media-server:4545/")
    assert submit.downloader_ui_url() == "http://media-server:4545/"
