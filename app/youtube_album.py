"""Resolve genuine YouTube Music album URLs, allowing edition and title variants.

Exact artist/album matches win; recognized edition suffixes rank next, then
arbitrary trailing parenthetical qualifiers. Unrelated titles, invalid URLs,
tracks, and videos never match.
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
    r"(?i)\b(?:complete|deluxe|remaster(?:ed|ing)?|expanded|anniversary|"
    r"collector(?:'s|s)?\s+edition|special\s+edition|limited\s+edition|"
    r"bonus\s+(?:track|tracks|disc|edition|version)|reissue|"
    r"original\s+recording\s+remastered)\b"
)
_BRACKET_SUFFIX = re.compile(r"^(?P<base>.+?)\s*[\[(](?P<tag>[^\])]{1,110})[\])]\s*$")
_DASH_SUFFIX = re.compile(r"^(?P<base>.+?)\s+[-–—]\s+(?P<tag>[^\[\]()]{1,110})\s*$")
_COMMA_SUFFIX = re.compile(r"^(?P<base>.+?),\s*(?P<tag>[^,\[\]()]{1,110})\s*$")
_TRAILING_PARENS = re.compile(r"^(?P<base>.+?)\s*\((?P<tag>[^()]{1,110})\)\s*$")


def base_album_title(title):
    """Remove *trailing* edition tags; preserve the actual album title.

    Examples: 'Hats (2002 Remaster)' -> 'Hats'
              'Hats (Live)' -> 'Hats (Live)' (a distinct release)
    """
    text = str(title or "").strip()
    for _ in range(5):
        match = (_BRACKET_SUFFIX.fullmatch(text)
                 or _DASH_SUFFIX.fullmatch(text)
                 or _COMMA_SUFFIX.fullmatch(text))
        if not match or not _EDITION_MARKER.search(match.group("tag")):
            break
        candidate = match.group("base").strip()
        if not candidate:
            break
        text = candidate
    return text


def parenthetical_base_album_title(title):
    """Strip any trailing (...) qualifiers, plus recognized edition suffixes.

    Other album words and non-trailing parentheses remain significant.
    This is a fallback match only: an exact title or known edition wins.
    """
    text = base_album_title(title)
    for _ in range(5):
        match = _TRAILING_PARENS.fullmatch(text)
        if not match:
            break
        text = base_album_title(match.group("base").strip())
    return text


def _requests_complete_release(title):
    """A complete reissue must not resolve to an abridged original release."""
    text = str(title or "").strip()
    # Recognize edition markers anywhere in a trailing sequence, including
    # ', Complete', '- Complete (Live)', and '(Complete Edition)'.
    for _ in range(5):
        parens = _TRAILING_PARENS.fullmatch(text)
        if parens:
            if re.search(r"(?i)\bcomplete\b", parens.group("tag")):
                return True
            text = parens.group("base").strip()
            continue
        suffix = (_BRACKET_SUFFIX.fullmatch(text)
                  or _DASH_SUFFIX.fullmatch(text)
                  or _COMMA_SUFFIX.fullmatch(text))
        if not suffix:
            return False
        if re.search(r"(?i)\bcomplete\b", suffix.group("tag")):
            return True
        text = suffix.group("base").strip()
    return False


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


# Jazz albums are frequently credited to the leader's Trio, Quartet, etc.
# rather than to the leader alone. Only these well-defined ensemble suffixes
# are accepted; collaborators or a band with a different name are not aliases.
_ENSEMBLE_NAMES = ("Trio", "Quartet", "Quintet", "Sextet", "Septet", "Octet", "Nonet")
_ENSEMBLE_SUFFIXES = {norm(label) for label in _ENSEMBLE_NAMES}
_ARTIST_SEPARATOR = re.compile(r"\s*(?:&|/|\band\b)\s*", re.IGNORECASE)


def _ensemble_credit_matches(artist, credited_name):
    """Match 'Isao Suzuki' to 'Isao Suzuki Trio & Isao Suzuki Quartet'.

    Do not accept collaborations with a different leader or arbitrary text
    beginning with the requested name.
    """
    expected = norm(artist)
    pieces = [norm(part) for part in _ARTIST_SEPARATOR.split(credited_name)]
    if not expected or not pieces or any(not part for part in pieces):
        return False
    for piece in pieces:
        if piece == expected:
            continue
        if piece.startswith(expected + " ") and piece[len(expected) + 1:] in _ENSEMBLE_SUFFIXES:
            continue
        # Discographical shorthand: 'Isao Suzuki Trio / Quartet'.
        if piece in _ENSEMBLE_SUFFIXES and len(pieces) > 1:
            continue
        return False
    return any(piece.startswith(expected + " ") for piece in pieces)


def _artist_match(artist, item):
    """Return (credit confidence, display artist) or None."""
    names = _artist_names(item)
    wanted = norm(artist)
    if any(norm(name) == wanted for name in names):
        return (0, next(name for name in names if norm(name) == wanted))
    # When multiple artist credits are present, ALL must be safely derived
    # from the artist's own ensemble name. Never match a mixed collaboration
    # solely because one member has a matching prefix.
    if names and all(_ensemble_credit_matches(artist, name) for name in names):
        return (1, names[0])
    return None


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
    want_parenthetical_base = norm(parenthetical_base_album_title(album))
    requires_complete = _requests_complete_release(album)
    if not want_artist or not want_parenthetical_base:
        return None

    candidates = []
    for index, item in enumerate(results):
        if not isinstance(item, dict) or item.get("resultType") != "album":
            continue
        artist_match = _artist_match(artist, item)
        if artist_match is None:
            continue
        artist_rank, actual_artist = artist_match
        url = _album_url(item)
        if not url:
            continue
        found_title = str(item.get("title") or "").strip()
        if requires_complete and not _requests_complete_release(found_title):
            continue
        found_exact = norm(found_title)
        if found_exact == want_exact:
            match_type = "exact"
            score = (0, artist_rank, 0, 0, index)
        elif want_base and norm(base_album_title(found_title)) == want_base:
            match_type = "edition"
            # If the user requested a particular edition, prefer candidates
            # with the same edition qualifier before other versions.
            want_tag = norm(album.removeprefix(base_album_title(album)).strip())
            found_tag = norm(found_title.removeprefix(base_album_title(found_title)).strip())
            same_qualifier = bool(want_tag and found_tag and want_tag == found_tag)
            score = (1, artist_rank, 0 if same_qualifier else 1, len(found_title), index)
        elif (want_parenthetical_base and
              norm(parenthetical_base_album_title(found_title)) == want_parenthetical_base):
            match_type = "parenthetical"
            score = (2, artist_rank, 0, len(found_title), index)
        else:
            continue
        candidates.append((score, {
            "status": "found", "url": url,
            "artist": actual_artist,
            "artist_match_type": "ensemble" if artist_rank else "exact",
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
    parenthetical_base = parenthetical_base_album_title(album)
    queries = [f"{artist} {album}"]
    if norm(base) != norm(album):
        queries.append(f"{artist} {base}")
    if norm(parenthetical_base) != norm(base):
        queries.append(f"{artist} {parenthetical_base}")
    if len(queries) == 1:
        # Occasionally the first album search is dominated by recordings,
        # compilations or similarly named releases. These targeted queries
        # let YouTube Music expose the deluxe/remastered catalog entry.
        queries.extend((f"{artist} {base} deluxe", f"{artist} {base} remastered"))

    # Some jazz releases are indexed under the leader's ensemble instead of
    # their name (e.g. Isao Suzuki Trio/Quartet, rather than Isao Suzuki).
    # These bounded follow-up queries are used only when normal searches fail.
    if not norm(artist).endswith(tuple(" " + norm(n) for n in _ENSEMBLE_NAMES)):
        queries.extend(f"{artist} {ensemble} {parenthetical_base}" for ensemble in ("Trio", "Quartet", "Quintet"))

    for query in dict.fromkeys(queries):
        results = catalog.search(query, filter="albums", limit=20)
        match = pick_exact_album(artist, album, results)
        if match:
            return match

    return {
        "status": "not_found",
        "message": "No matching album or title variant in the YouTube Music catalog.",
    }


def lookup_album(artist, album):
    artist = artist.strip()
    album = album.strip()
    if not artist or not album or len(artist) > 200 or len(album) > 250:
        raise ValueError("Enter an artist and album title.")
    return _lookup(artist, album)
