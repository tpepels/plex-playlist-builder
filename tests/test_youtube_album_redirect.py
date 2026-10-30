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


def test_found_album_still_opens_verified_album_url(monkeypatch):
    direct_url = "https://music.youtube.com/playlist?list=OLAK5uy_verified"
    monkeypatch.setattr(
        main, "youtube_album",
        lambda _artist, _album: {"status": "found", "url": direct_url},
    )

    response = main.open_youtube_album("Stan Getz", "Getz at the Gate")

    assert response.status_code == 302
    assert response.headers["location"] == direct_url


def test_not_found_api_result_remains_available(monkeypatch):
    expected = {"status": "not_found", "message": "No matching album."}
    monkeypatch.setattr(main, "lookup_album", lambda _artist, _album: expected)

    assert main.youtube_album("Stan Getz", "Getz at the Gate") == expected
