"""One-click links must be read-only; signed POST is the only download action."""
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import main


@pytest.mark.parametrize("artist,album", [
    ("Stan Getz", "Getz at the Gate"),
    ("Blossom Dearie", "Blossom Time at Ronnie Scott's"),
    ("鈴木勲", "Blow Up (Deluxe Edition)"),
])
def test_missing_album_opens_search_without_queue(monkeypatch, artist, album):
    monkeypatch.setattr(main, "youtube_album", lambda a, b: {"status": "not_found"})
    monkeypatch.setattr(main, "queue_album", lambda *args: pytest.fail("GET queued a download"))
    response = main.open_youtube_album(artist, album)
    assert response.status_code == 302
    loc = urlparse(response.headers["location"])
    assert (loc.netloc, loc.path) == ("music.youtube.com", "/search")
    assert parse_qs(loc.query) == {"q": [f"{artist} {album}"]}


def test_resolved_album_get_requires_explicit_post(monkeypatch):
    monkeypatch.setattr(main, "youtube_album", lambda a, b: {
        "status": "found", "url": "https://music.youtube.com/browse/MPREgood",
        "artist": "Stan Getz", "album": "Getz at the Gate (Live)",
        "match_type": "explicit_alias",
    })
    monkeypatch.setattr(main, "queue_album", lambda *args: pytest.fail("GET queued"))
    response = main.open_youtube_album("Stan Getz", "Getz at the Gate")
    assert response.status_code == 200
    page = response.body.decode()
    assert 'method="post" action="/queue-youtube-album"' in page
    assert "Getz at the Gate (Live)" in page
    assert "Queue on yt-dlp" in page
    assert response.headers["cache-control"] == "no-store"


def test_ambiguous_catalog_results_never_queue(monkeypatch):
    monkeypatch.setattr(main, "youtube_album", lambda a, b: {
        "status": "ambiguous",
        "candidates": [
            {"artist": "Artist", "album": "Duplicate", "url": "https://music.youtube.com/browse/MPREone"},
            {"artist": "Artist", "album": "Duplicate", "url": "https://music.youtube.com/browse/MPREtwo"},
        ],
    })
    monkeypatch.setattr(main, "queue_album", lambda *args: pytest.fail("ambiguous GET queued"))
    response = main.open_youtube_album("Artist", "Duplicate")
    assert response.status_code == 409
    assert "No download queued" in response.body.decode()


def _request(body):
    data = body.encode("utf-8")
    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/queue-youtube-album",
                    "headers": []}, receive)


@pytest.mark.asyncio
async def test_signed_post_queues_once_and_returns_job_identity(monkeypatch):
    monkeypatch.setattr(main, "youtube_album", lambda a, b: {
        "status": "found", "url": "https://music.youtube.com/browse/MPREgood",
        "artist": "Stan Getz", "album": "Getz at the Gate (Live)",
        "match_type": "explicit_alias", "normalization_rules": ["explicit_album_alias"],
        "queries": ["Stan Getz Getz at the Gate"],
    })
    monkeypatch.setattr(main, "_owned_album", lambda a, b: False)
    sent = []
    def queue(url, audit):
        sent.append((url, audit))
        return {"job_id": "job123", "duplicate": False, "status": "queued"}
    monkeypatch.setattr(main, "queue_album", queue)
    monkeypatch.setattr(main, "downloader_ui_url", lambda: "http://media-server:4545/")
    token = main._album_action_token("Stan Getz", "Getz at the Gate", int(main.time.time() // 3600))
    response = await main.queue_youtube_album(
        _request(f"artist=Stan+Getz&album=Getz+at+the+Gate&token={token}"))
    assert response.status_code == 303
    assert response.headers["x-album-job-id"] == "job123"
    assert sent[0][1]["match_type"] == "explicit_alias"


@pytest.mark.asyncio
async def test_bad_post_token_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "queue_album", lambda *args: pytest.fail("invalid token queued"))
    with pytest.raises(HTTPException) as ex:
        await main.queue_youtube_album(
            _request("artist=Stan+Getz&album=Getz+at+the+Gate&token=bogus"))
    assert ex.value.status_code == 403


@pytest.mark.asyncio
async def test_owned_album_blocks_automatic_download(monkeypatch):
    monkeypatch.setattr(main, "youtube_album", lambda a, b: {
        "status": "found", "url": "https://music.youtube.com/browse/MPREgood",
        "artist": a, "album": b, "match_type": "exact",
    })
    monkeypatch.setattr(main, "_owned_album", lambda a, b: True)
    monkeypatch.setattr(main, "queue_album", lambda *args: pytest.fail("owned album queued"))
    token = main._album_action_token("A", "B", int(main.time.time() // 3600))
    with pytest.raises(HTTPException) as ex:
        await main.queue_youtube_album(_request(f"artist=A&album=B&token={token}"))
    assert ex.value.status_code == 409


def test_not_found_api_remains_read_only(monkeypatch):
    expected = {"status": "not_found", "message": "Not found"}
    monkeypatch.setattr(main, "lookup_album", lambda a, b: expected)
    assert main.youtube_album("Stan Getz", "Getz at the Gate") == expected
