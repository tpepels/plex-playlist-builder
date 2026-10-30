"""Fast HTTP regressions for immediate album progress and signed submission."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app import main


@pytest.mark.parametrize("artist,album", [
    ("Jim Hall", "Jim Hall Live!"),
    ("Stan Getz", "Getz at the Gate"),
    ("鈴木勲", "Blow Up (Deluxe Edition)"),
])
def test_open_album_link_renders_immediately_without_catalog_or_plex(monkeypatch, artist, album):
    def never(*args, **kwargs):
        pytest.fail("GET blocked on YouTube Music, Plex, or downloader")
    monkeypatch.setattr(main, "youtube_album", never)
    monkeypatch.setattr(main, "_owned_album", never)
    monkeypatch.setattr(main, "queue_album", never)

    response = main.open_youtube_album(artist, album)
    page = response.body.decode("utf-8")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Searching YouTube Music" in page
    assert "Elapsed: 0 s" in page
    assert "Checking Plex and contacting yt-dlp" in page
    assert 'id="queue-album-form" method="post" action="/queue-youtube-album"' in page
    assert "document.getElementById" in page
    assert artist.replace("&", "&amp;") in page


def test_not_found_search_url_is_exact_artist_and_album(monkeypatch):
    response = main.open_youtube_album("Stan Getz", "Getz at the Gate")
    page = response.body.decode()
    assert "https://music.youtube.com/search?q=Stan+Getz+Getz+at+the+Gate" in page
    # No server-side catalog request; client decides after /api/youtube-album.
    assert 'const artist = "Stan Getz";' in page


@pytest.mark.parametrize("artist,album", [("", "Album"), ("Artist", " "), ("A" * 201, "B")])
def test_invalid_get_input_returns_422(artist, album):
    with pytest.raises(HTTPException) as error:
        main.open_youtube_album(artist, album)
    assert error.value.status_code == 422


def _request(body, accept=""):
    data = body.encode("utf-8")
    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    headers = [(b"accept", accept.encode())] if accept else []
    return Request({"type": "http", "method": "POST", "path": "/queue-youtube-album",
                    "headers": headers}, receive)


def _matched():
    return {
        "status": "found", "url": "https://music.youtube.com/browse/MPREb_IObRABnIPO3",
        "artist": "Jim Hall", "album": "Live!", "match_type": "artist_prefix",
        "artist_match_type": "exact",
        "normalization_rules": ["redundant_artist_prefix"],
        "queries": ["Jim Hall live"],
    }


def test_ajax_post_reports_queued_job_id_and_status(monkeypatch):
    monkeypatch.setattr(main, "youtube_album", lambda a, b: _matched())
    monkeypatch.setattr(main, "_owned_album", lambda a, b: False)
    queued = []
    def queue(url, audit):
        queued.append((url, audit))
        return {"job_id": "job123", "duplicate": False, "status": "queued"}
    monkeypatch.setattr(main, "queue_album", queue)
    monkeypatch.setattr(main, "downloader_ui_url", lambda: "http://media-server:4545/")
    token = main._album_action_token("Jim Hall", "Jim Hall Live!", int(main.time.time() // 3600))
    response = asyncio.run(main.queue_youtube_album(_request(
        f"artist=Jim+Hall&album=Jim+Hall+Live%21&token={token}", "application/json")))
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload == {
        "ok": True, "job_id": "job123", "status": "queued",
        "duplicate": False, "downloader_url": "http://media-server:4545/",
    }
    assert queued[0][1]["normalization_rules"] == ["redundant_artist_prefix"]
    assert response.headers["cache-control"] == "no-store"


def test_ajax_post_reports_existing_job_without_requeue_ambiguity(monkeypatch):
    monkeypatch.setattr(main, "youtube_album", lambda a, b: _matched())
    monkeypatch.setattr(main, "_owned_album", lambda a, b: False)
    monkeypatch.setattr(main, "queue_album", lambda *args: {
        "job_id": "existing", "duplicate": True, "status": "succeeded"})
    token = main._album_action_token("Jim Hall", "Jim Hall Live!", int(main.time.time() // 3600))
    response = asyncio.run(main.queue_youtube_album(_request(
        f"artist=Jim+Hall&album=Jim+Hall+Live%21&token={token}", "application/json")))
    payload = json.loads(response.body)
    assert payload["job_id"] == "existing"
    assert payload["duplicate"] is True
    assert payload["status"] == "succeeded"


def test_non_ajax_post_still_redirects_after_enqueue(monkeypatch):
    monkeypatch.setattr(main, "youtube_album", lambda a, b: _matched())
    monkeypatch.setattr(main, "_owned_album", lambda a, b: False)
    monkeypatch.setattr(main, "queue_album", lambda *args: {
        "job_id": "manual", "duplicate": False, "status": "queued"})
    token = main._album_action_token("Jim Hall", "Jim Hall Live!", int(main.time.time() // 3600))
    response = asyncio.run(main.queue_youtube_album(_request(
        f"artist=Jim+Hall&album=Jim+Hall+Live%21&token={token}")))
    assert response.status_code == 303
    assert response.headers["x-album-job-id"] == "manual"


def test_bad_post_token_is_rejected(monkeypatch):
    monkeypatch.setattr(main, "queue_album", lambda *args: pytest.fail("invalid token queued"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.queue_youtube_album(
            _request("artist=Jim+Hall&album=Jim+Hall+Live%21&token=bogus", "application/json")))
    assert error.value.status_code == 403


def test_owned_album_blocks_download(monkeypatch):
    monkeypatch.setattr(main, "youtube_album", lambda a, b: _matched())
    monkeypatch.setattr(main, "_owned_album", lambda a, b: True)
    monkeypatch.setattr(main, "queue_album", lambda *args: pytest.fail("owned album queued"))
    token = main._album_action_token("Jim Hall", "Jim Hall Live!", int(main.time.time() // 3600))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.queue_youtube_album(_request(
            f"artist=Jim+Hall&album=Jim+Hall+Live%21&token={token}", "application/json")))
    assert error.value.status_code == 409


def test_resolver_api_remains_read_only(monkeypatch):
    expected = {"status": "not_found", "message": "Not found"}
    monkeypatch.setattr(main, "lookup_album", lambda a, b: expected)
    assert main.youtube_album("Jim Hall", "Jim Hall Live!") == expected
