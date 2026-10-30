"""Communicate with the idempotent album-job API of yt-dlp-server."""
from __future__ import annotations

from functools import lru_cache
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


class AlbumQueueError(Exception):
    """The downloader could not confirm the album's existing/new job."""


def _valid_album_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "music.youtube.com":
        return False
    if parsed.path == "/playlist":
        playlist = parse_qs(parsed.query).get("list", [""])[0]
        return bool(re.fullmatch(r"OLAK5uy_[A-Za-z0-9_-]+", playlist))
    if parsed.path.startswith("/browse/") and not parsed.query:
        return bool(re.fullmatch(r"/browse/MPRE[A-Za-z0-9_-]+", parsed.path))
    return False


@lru_cache(maxsize=128)
def _download_url(url: str) -> str:
    """Prefer the YouTube Music audio playlist ID over an album browse URL."""
    parsed = urlparse(url)
    if not parsed.path.startswith("/browse/"):
        return url
    try:
        from ytmusicapi import YTMusic
        record = YTMusic().get_album(parsed.path.rsplit("/", 1)[-1])
        playlist_id = record.get("audioPlaylistId")
        if isinstance(playlist_id, str) and re.fullmatch(r"OLAK5uy_[A-Za-z0-9_-]+", playlist_id):
            return "https://music.youtube.com/playlist?" + urlencode({"list": playlist_id})
    except Exception:
        pass
    return url


def _server_url() -> str:
    server = os.getenv("YTDLP_SERVER_URL", "http://yt-dlp-server:4545").rstrip("/")
    parsed = urlparse(server)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
        raise AlbumQueueError("YTDLP_SERVER_URL must be a server origin.")
    return server


def queue_album(url: str, audit: dict | None = None) -> dict:
    """Return {job_id, duplicate, status}; a timeout never means 'queued'."""
    if not _valid_album_url(url):
        raise AlbumQueueError("Album resolver returned an invalid download URL.")
    url = _download_url(url)
    payload = json.dumps({"url": url, "match_audit": audit or {}}).encode("utf-8")
    request = Request(
        _server_url() + "/api/albums", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 429:
            raise AlbumQueueError("The yt-dlp download queue is full.") from exc
        raise AlbumQueueError(f"The yt-dlp server refused the job (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise AlbumQueueError("The downloader did not confirm the job. Repeating the same request is safe.") from exc
    if not isinstance(result, dict) or not result.get("ok") or not result.get("job_id"):
        raise AlbumQueueError("The downloader returned no valid job acknowledgement.")
    return {
        "job_id": result["job_id"], "duplicate": bool(result.get("duplicate")),
        "status": result.get("status", "unknown"),
    }


def downloader_ui_url() -> str:
    return os.getenv("YTDLP_PUBLIC_URL", "http://media-server:4545").rstrip("/") + "/"
