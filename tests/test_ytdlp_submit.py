"""Fast, isolated tests for sending a resolved album to yt-dlp."""
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import pytest

from app import ytdlp_submit as submit


@pytest.mark.parametrize("album_url", [
    "https://music.youtube.com/playlist?list=OLAK5uy_verified",
    "https://music.youtube.com/browse/MPREb_l3o4MNZF1rh",
])
def test_verified_album_posts_to_existing_download_form(monkeypatch, album_url):
    recorded = []

    class Downloader:
        def open(self, request, timeout):
            recorded.append((request, timeout))
            raise HTTPError(request.full_url, 302, "Found", {"Location": "/"}, None)

    monkeypatch.setenv("YTDLP_SERVER_URL", "http://yt-dlp-server:4545")
    monkeypatch.setattr(submit, "_download_url", lambda url: url)
    monkeypatch.setattr(submit, "build_opener", lambda handler: Downloader())
    submit.queue_album(album_url)

    assert len(recorded) == 1
    request, timeout = recorded[0]
    assert request.full_url == "http://yt-dlp-server:4545/download"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert parse_qs(request.data.decode()) == {"url": [album_url], "playlist": ["on"]}
    assert timeout == 45


@pytest.mark.parametrize("album_url", [
    "https://music.youtube.com/search?q=Stan+Getz",
    "https://music.youtube.com/watch?v=bad",
    "https://evil.example/playlist?list=OLAK5uy_evil",
    "http://music.youtube.com/playlist?list=OLAK5uy_wrong_scheme",
    "https://music.youtube.com/playlist?list=OTHER_ID",
])
def test_non_album_or_non_youtube_music_urls_never_queued(monkeypatch, album_url):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access was attempted")
    monkeypatch.setattr(submit, "build_opener", forbidden)
    with pytest.raises(submit.AlbumQueueError, match="invalid download URL"):
        submit.queue_album(album_url)


@pytest.mark.parametrize("code,location,detail", [
    (429, None, "queue is full"),
    (400, None, "HTTP 400"),
    (302, "https://other.example/", "HTTP 302"),
])
def test_downloader_rejection_does_not_report_queued(monkeypatch, code, location, detail):
    class Downloader:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, code, "Rejected",
                            {"Location": location} if location else {}, None)
    monkeypatch.setattr(submit, "build_opener", lambda handler: Downloader())

    with pytest.raises(submit.AlbumQueueError, match=detail):
        submit.queue_album("https://music.youtube.com/browse/MPREvalid")


def test_unreachable_downloader_does_not_report_queued(monkeypatch):
    class Downloader:
        def open(self, request, timeout):
            raise URLError("connection refused")
    monkeypatch.setattr(submit, "build_opener", lambda handler: Downloader())

    with pytest.raises(submit.AlbumQueueError, match="could not be reached"):
        submit.queue_album("https://music.youtube.com/browse/MPREvalid")


def test_browser_redirect_uses_public_address_not_docker_hostname(monkeypatch):
    monkeypatch.setenv("YTDLP_PUBLIC_URL", "http://media-server:4545/")
    assert submit.downloader_ui_url() == "http://media-server:4545/"


def test_browse_album_converts_to_yt_dlp_compatible_olak_playlist(monkeypatch):
    import sys
    from types import SimpleNamespace

    calls = []
    def get_album(album_id):
        calls.append(album_id)
        return {"audioPlaylistId": "OLAK5uy_verifiedPlaylist"}

    monkeypatch.setitem(
        sys.modules, "ytmusicapi",
        SimpleNamespace(YTMusic=lambda: SimpleNamespace(get_album=get_album)),
    )
    submit._download_url.cache_clear()
    try:
        actual = submit._download_url("https://music.youtube.com/browse/MPREb_l3o4MNZF1rh")
        assert actual == "https://music.youtube.com/playlist?list=OLAK5uy_verifiedPlaylist"
        assert calls == ["MPREb_l3o4MNZF1rh"]
    finally:
        submit._download_url.cache_clear()


def test_browse_album_keeps_verified_url_if_playlist_id_unavailable(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(
        sys.modules, "ytmusicapi",
        SimpleNamespace(YTMusic=lambda: SimpleNamespace(
            get_album=lambda album_id: {"audioPlaylistId": "invalid"}
        )),
    )
    submit._download_url.cache_clear()
    try:
        original = "https://music.youtube.com/browse/MPREb_l3o4MNZF1rh"
        assert submit._download_url(original) == original
    finally:
        submit._download_url.cache_clear()
