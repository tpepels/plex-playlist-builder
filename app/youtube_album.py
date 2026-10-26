"""Resolve *actual* YouTube Music album URLs, never fabricated search URLs.

A link is returned only after a strict album + artist catalog match. Availability
may vary by region; a public catalog match does not guarantee local playback.
"""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _artist_name(item):
    a = item.get("artist")
    if a:
        return a
    artists = item.get("artists") or []
    return artists[0].get("name", "") if artists else ""


def pick_exact_album(artist, album, results):
    """Never turn a fuzzy/track/video match into a false direct album link."""
    want_artist, want_album = norm(artist), norm(album)
    if not want_artist or not want_album:
        return None
    for item in results:
        if not isinstance(item, dict) or item.get("resultType") != "album":
            continue
        if norm(item.get("title")) != want_album or norm(_artist_name(item)) != want_artist:
            continue
        playlist_id = item.get("playlistId") or ""
        browse_id = item.get("browseId") or ""
        if re.fullmatch(r"OLAK5uy_[A-Za-z0-9_-]+", playlist_id):
            url = "https://music.youtube.com/playlist?list=" + playlist_id
        elif re.fullmatch(r"MPRE[A-Za-z0-9_-]+", browse_id):
            url = "https://music.youtube.com/browse/" + browse_id
        else:
            continue
        return {"status": "found", "url": url,
                "artist": _artist_name(item), "album": item["title"],
                "year": item.get("year"), "catalog_verified": True}
    return None


@lru_cache(maxsize=256)
def _lookup(artist, album):
    from ytmusicapi import YTMusic
    results = YTMusic().search(f"{artist} {album}", filter="albums", limit=20)
    return pick_exact_album(artist, album, results) or {
        "status": "not_found",
        "message": "No exact album match in the YouTube Music catalog. No link was guessed.",
    }


def lookup_album(artist, album):
    artist = artist.strip()
    album = album.strip()
    if not artist or not album or len(artist) > 200 or len(album) > 250:
        raise ValueError("Enter an artist and album title.")
    return _lookup(artist, album)
