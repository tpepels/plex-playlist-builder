"""Queue a verified YouTube Music album on the existing yt-dlp server.

The downloader's /download form endpoint accepts a URL and playlist=on, then
redirects to / only *after* the job has been admitted to its queue.
"""
from __future__ import annotations

from functools import lru_cache
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class AlbumQueueError(Exception):
    """The album was not confirmed queued by the yt-dlp server."""


class _StayOnSubmit(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
    """Prefer the OLAK playlist equivalent of a YouTube Music browse album.

    yt-dlp can fail while resolving MPRE browse IDs itself. YTMusic exposes
    the album's native audioPlaylistId, which the downloader also recognizes
    as an album collection. If enrichment fails, preserve the verified URL.
    """
    parsed = urlparse(url)
    if not parsed.path.startswith("/browse/"):
        return url
    try:
        from ytmusicapi import YTMusic
        album_id = parsed.path.rsplit("/", 1)[-1]
        record = YTMusic().get_album(album_id)
        playlist_id = record.get("audioPlaylistId")
        if isinstance(playlist_id, str) and re.fullmatch(r"OLAK5uy_[A-Za-z0-9_-]+", playlist_id):
            return "https://music.youtube.com/playlist?" + urlencode({"list": playlist_id})
    except Exception:
        pass  # The original verified MPRE album may still work with yt-dlp.
    return url


def queue_album(url: str) -> None:
    """Submit a verified album once; raise unless the queue confirms acceptance."""
    if not _valid_album_url(url):
        raise AlbumQueueError("Album resolver returned an invalid download URL.")
    url = _download_url(url)

    server = os.getenv("YTDLP_SERVER_URL", "http://yt-dlp-server:4545").rstrip("/")
    parsed_server = urlparse(server)
    if parsed_server.scheme not in {"http", "https"} or not parsed_server.netloc or parsed_server.path:
        raise AlbumQueueError("YTDLP_SERVER_URL must be a server origin.")

    request = Request(
        server + "/download",
        data=urlencode({"url": url, "playlist": "on"}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    opener = build_opener(_StayOnSubmit())
    try:
        with opener.open(request, timeout=45):
            # The current yt-dlp server returns 302 on a successful enqueue.
            raise AlbumQueueError("The downloader did not confirm that the album was queued.")
    except HTTPError as exc:
        if exc.code == 302 and exc.headers.get("Location") == "/":
            return
        if exc.code == 429:
            raise AlbumQueueError("The yt-dlp download queue is full.") from exc
        raise AlbumQueueError(f"The yt-dlp server rejected the album (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AlbumQueueError("The yt-dlp server could not be reached or timed out.") from exc


def downloader_ui_url() -> str:
    """Browser-reachable URL; internal Docker DNS must never enter redirects."""
    return os.getenv("YTDLP_PUBLIC_URL", "http://media-server:4545").rstrip("/") + "/"
