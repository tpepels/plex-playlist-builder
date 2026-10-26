"""Resolve genuine YouTube Music album URLs, allowing common edition suffixes.

A match must have the requested artist and the same album title once ONLY
recognizable release/edition qualifiers have been removed. Tracks, videos,
unrelated titles and unvalidated URLs are never accepted.
"""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


# Conservative edition language: do NOT strip "(Live)", "(Acoustic)", album
# subtitles, volume numbers, or arbitrary parenthesized text. Those can be
# legitimately different albums, not editions of the same release.
_EDITION_MARKER = re.compile(
    r"(?i)\b(?:deluxe|remaster(?:ed|ing)?|expanded|anniversary|"
    r"collector(?:'s|s)?\s+edition|special\s+edition|limited\s+edition|"
    r"bonus\s+(?:track|tracks|disc|edition|version)|reissue|"
    r"original\s+recording\s+remastered)\b"
)
_BRACKET_SUFFIX = re.compile(r"^(?P<base>.+?)\s*[\[(](?P<tag>[^\])]{1,110})[\])]\s*$")
_DASH_SUFFIX = re.compile(r"^(?P<base>.+?)\s+[-–—]\s+(?P<tag>[^\[\]()]{1,110})\s*$")


def base_album_title(title):
    """Remove *trailing* edition tags; preserve the actual album title.

    Examples: 'Hats (2002 Remaster)' -> 'Hats'
              'Hats (Live)' -> 'Hats (Live)' (a distinct release)
    """
    text = str(title or "").strip()
    for _ in range(5):
        match = _BRACKET_SUFFIX.fullmatch(text) or _DASH_SUFFIX.fullmatch(text)
        if not match or not _EDITION_MARKER.search(match.group("tag")):
            break
        candidate = match.group("base").strip()
        if not candidate:
            break
        text = candidate
    return text


def _artist_names(item):
    artists = []
    artist = item.get("artist")
    if isinstance(artist, str):
        artists.append(artist)
    elif isinstance(artist, dict):
        artists.append(artist.get("name", ""))
    for other in item.get("artists") or []:
        if isinstance(other, str):
            artists.append(other)
        elif isinstance(other, dict):
            artists.append(other.get("name", ""))
    return [name for name in artists if name]


def _album_url(item):
    playlist_id = item.get("playlistId") or ""
    browse_id = item.get("browseId") or ""
    if isinstance(playlist_id, str) and re.fullmatch(r"OLAK5uy_[A-Za-z0-9_-]+", playlist_id):
        return "https://music.youtube.com/playlist?list=" + playlist_id
    if isinstance(browse_id, str) and re.fullmatch(r"MPRE[A-Za-z0-9_-]+", browse_id):
        return "https://music.youtube.com/browse/" + browse_id
    return None


def pick_exact_album(artist, album, results):
    """Return the closest safe album match (exact preferred over edition).

    Kept under its original function name for callers of the earlier resolver.
    """
    want_artist, want_exact = norm(artist), norm(album)
    want_base = norm(base_album_title(album))
    if not want_artist or not want_base:
        return None

    candidates = []
    for index, item in enumerate(results):
        if not isinstance(item, dict) or item.get("resultType") != "album":
            continue
        if not any(norm(name) == want_artist for name in _artist_names(item)):
            continue
        url = _album_url(item)
        if not url:
            continue
        found_title = str(item.get("title") or "").strip()
        found_exact = norm(found_title)
        if found_exact == want_exact:
            match_type = "exact"
            score = (0, 0, 0, index)
        elif want_base and norm(base_album_title(found_title)) == want_base:
            match_type = "edition"
            # If the user requested a particular edition, prefer candidates
            # with the same edition qualifier before other versions.
            want_tag = norm(album.removeprefix(base_album_title(album)).strip())
            found_tag = norm(found_title.removeprefix(base_album_title(found_title)).strip())
            same_qualifier = bool(want_tag and found_tag and want_tag == found_tag)
            score = (1, 0 if same_qualifier else 1, len(found_title), index)
        else:
            continue
        candidates.append((score, {
            "status": "found", "url": url,
            "artist": next((name for name in _artist_names(item) if norm(name) == want_artist), artist),
            "album": found_title,
            "requested_album": album,
            "match_type": match_type,
            "year": item.get("year"), "catalog_verified": True,
        }))

    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


@lru_cache(maxsize=256)
def _lookup(artist, album):
    from ytmusicapi import YTMusic

    catalog = YTMusic()
    base = base_album_title(album)
    queries = [f"{artist} {album}"]
    if norm(base) != norm(album):
        queries.append(f"{artist} {base}")
    else:
        # Occasionally the first album search is dominated by recordings,
        # compilations or similarly named releases. These targeted queries
        # let YouTube Music expose the deluxe/remastered catalog entry.
        queries.extend((f"{artist} {base} deluxe", f"{artist} {base} remastered"))

    for query in dict.fromkeys(queries):
        results = catalog.search(query, filter="albums", limit=20)
        match = pick_exact_album(artist, album, results)
        if match:
            return match

    return {
        "status": "not_found",
        "message": "No matching album or recognized edition in the YouTube Music catalog.",
    }


def lookup_album(artist, album):
    artist = artist.strip()
    album = album.strip()
    if not artist or not album or len(artist) > 200 or len(album) > 250:
        raise ValueError("Enter an artist and album title.")
    return _lookup(artist, album)
