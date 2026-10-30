"""Tests for the one-click YouTube Music album redirect."""
from urllib.parse import parse_qs, urlparse

import pytest

from app import main


@pytest.mark.parametrize("artist, album", [
    ("Stan Getz", "Getz at the Gate"),
    ("Blossom Dearie", "Blossom Time at Ronnie Scott's"),
    ("鈴木勲", "Blow Up (Deluxe Edition)"),
])
def test_missing_album_opens_exact_artist_album_search(monkeypatch, artist, album):
    monkeypatch.setattr(
        main, "youtube_album",
        lambda _artist, _album: {"status": "not_found", "message": "No matching album."},
    )

    response = main.open_youtube_album(artist, album)

    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert (location.scheme, location.netloc, location.path) == (
        "https", "music.youtube.com", "/search",
    )
    assert parse_qs(location.query) == {"q": [f"{artist} {album}"]}


def test_found_album_queues_verified_url_and_opens_downloader(monkeypatch):
    direct_url = "https://music.youtube.com/playlist?list=OLAK5uy_verified"
    queued = []
    monkeypatch.setattr(
        main, "youtube_album",
        lambda _artist, _album: {"status": "found", "url": direct_url},
    )
    monkeypatch.setattr(main, "queue_album", queued.append)
    monkeypatch.setattr(main, "downloader_ui_url", lambda: "http://media-server:4545/")

    response = main.open_youtube_album("Stan Getz", "Getz at the Gate")

    assert queued == [direct_url]
    assert response.status_code == 303
    assert response.headers["location"] == "http://media-server:4545/"
    assert response.headers["cache-control"] == "no-store"


def test_downloader_failure_is_reported_without_claiming_success(monkeypatch):
    from fastapi import HTTPException
    from app.ytdlp_submit import AlbumQueueError

    monkeypatch.setattr(
        main, "youtube_album",
        lambda _artist, _album: {"status": "found", "url": "https://music.youtube.com/browse/MPREalbum"},
    )
    def unavailable(url):
        raise AlbumQueueError("The yt-dlp download queue is full.")
    monkeypatch.setattr(main, "queue_album", unavailable)

    with pytest.raises(HTTPException) as error:
        main.open_youtube_album("Stan Getz", "Getz at the Gate")
    assert error.value.status_code == 502
    assert "queue is full" in error.value.detail


def test_not_found_api_result_remains_available(monkeypatch):
    expected = {"status": "not_found", "message": "No matching album."}
    monkeypatch.setattr(main, "lookup_album", lambda _artist, _album: expected)

    assert main.youtube_album("Stan Getz", "Getz at the Gate") == expected
