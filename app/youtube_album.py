"""Deterministic YouTube Music album resolver.

Only documented spelling/edition rules and explicit album-specific aliases
are permitted. Search result order never decides what is downloaded.
"""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata


def norm(value):
    """Canonical punctuation and explicitly equivalent word abbreviations."""
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", value)
    aliases = {"vol": "volume", "pt": "part"}
    return " ".join(aliases.get(word, word) for word in words)


# Release qualifiers are stripped ONLY at title boundaries and ONLY on this
# list. (Live), (Demo), (Acoustic), (Mono), (Stereo) and volume numbers stay.
_EDITION_TAG = re.compile(
    r"(?i)^(?:(?:the\s+)?complete(?:\s+(?:edition|recordings|collection))?|"
    r"deluxe(?:\s+(?:edition|version))?|"
    r"(?:\d{4}\s+)?remaster(?:ed|ing)?(?:\s+\d{4})?|"
    r"expanded(?:\s+edition)?|anniversary(?:\s+edition)?|"
    r"\d+(?:st|nd|rd|th)\s+anniversary(?:\s+edition)?|"
    r"special\s+edition|limited\s+edition|collector(?:'s|s)?\s+edition|"
    r"bonus\s+(?:track|tracks|disc|edition|version)|reissue|"
    r"original\s+recording\s+remastered)$"
)
_BRACKET_SUFFIX = re.compile(r"^(?P<base>.+?)\s*[\[(](?P<tag>[^\]\)]{1,110})[\])]\s*$")
_DASH_SUFFIX = re.compile(r"^(?P<base>.+?)\s+[-–—]\s+(?P<tag>[^\[\]()]{1,110})\s*$")
_COMMA_SUFFIX = re.compile(r"^(?P<base>.+?),\s*(?P<tag>[^,\[\]()]{1,110})\s*$")
_TRAILING_PARENS = re.compile(r"^(?P<base>.+?)\s*\((?P<tag>[^()]{1,110})\)\s*$")


def _edition_parts(title):
    """Return base title and removed release labels in source order."""
    text = str(title or "").strip()
    tags = []
    for _ in range(6):
        match = (_BRACKET_SUFFIX.fullmatch(text) or
                 _DASH_SUFFIX.fullmatch(text) or
                 _COMMA_SUFFIX.fullmatch(text))
        if not match or not _EDITION_TAG.fullmatch(match.group("tag").strip()):
            break
        base = match.group("base").strip()
        if not base:
            break
        tags.append(norm(match.group("tag")))
        text = base
    return text, tuple(tags)


def base_album_title(title):
    return _edition_parts(title)[0]


def parenthetical_base_album_title(title):
    """Compatibility API: arbitrary (...) text is no longer stripped."""
    return base_album_title(title)


def _requests_complete_release(title):
    return any("complete" in tag.split() for tag in _edition_parts(title)[1])


# This explicit catalog-alias whitelist is intentionally small. A real album
# recorded live can be labeled "(Live)" by the catalog, but the same rule is
# NOT applied to studio albums or to arbitrary other titles.
_EXPLICIT_ALIASES = (
    ("Stan Getz", "Getz at the Gate", "Getz at the Gate (Live)"),
    ("Miles Davis", "In Person Friday And Saturday Nights At The Blackhawk, Complete",
     "In Person Friday And Saturday Nights At The Blackhawk - Complete (Live)"),
)


def _explicit_alias(artist, requested, found):
    for credit, left, right in _EXPLICIT_ALIASES:
        if norm(credit) != norm(artist):
            continue
        if (norm(requested), norm(found)) in ((norm(left), norm(right)), (norm(right), norm(left))):
            return True
    return False


def _artist_names(item):
    names = []
    artist = item.get("artist")
    if isinstance(artist, str):
        names.append(artist)
    elif isinstance(artist, dict):
        names.append(artist.get("name", ""))
    for other in item.get("artists") or []:
        if isinstance(other, str):
            names.append(other)
        elif isinstance(other, dict):
            names.append(other.get("name", ""))
    return [name for name in names if name]


_ENSEMBLE_NAMES = ("Trio", "Quartet", "Quintet", "Sextet", "Septet", "Octet", "Nonet")
_ENSEMBLE_SUFFIXES = {norm(name) for name in _ENSEMBLE_NAMES}
_ARTIST_SEPARATOR = re.compile(r"\s*(?:&|/|\band\b)\s*", re.IGNORECASE)


def _ensemble_credit_matches(artist, credited_name):
    expected = norm(artist)
    pieces = [norm(part) for part in _ARTIST_SEPARATOR.split(credited_name)]
    if not expected or not pieces or any(not part for part in pieces):
        return False
    for piece in pieces:
        if piece == expected:
            continue
        if piece.startswith(expected + " ") and piece[len(expected) + 1:] in _ENSEMBLE_SUFFIXES:
            continue
        if piece in _ENSEMBLE_SUFFIXES and len(pieces) > 1:
            continue
        return False
    return any(piece.startswith(expected + " ") for piece in pieces)


def _artist_match(artist, item):
    names = _artist_names(item)
    wanted = norm(artist)
    if any(norm(name) == wanted for name in names):
        return (0, next(name for name in names if norm(name) == wanted))
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


def _rank_title(artist, requested, found):
    if norm(requested) == norm(found):
        return 0, "exact", ("case_punctuation",)
    wanted_base, wanted_tags = _edition_parts(requested)
    found_base, found_tags = _edition_parts(found)
    if norm(wanted_base) == norm(found_base) and (wanted_tags or found_tags):
        # A request for a complete collection never resolves to the short one.
        if _requests_complete_release(requested) and not _requests_complete_release(found):
            return None
        # An explicit (Live)/(Demo)/(Vol.2) on either side is part of the
        # *base* and therefore cannot disappear through edition normalization.
        if wanted_tags and found_tags and set(wanted_tags) == set(found_tags):
            return 1, "edition", ("edition_suffix", "same_edition")
        return 2, "edition", ("edition_suffix",)
    if _explicit_alias(artist, requested, found):
        return 3, "explicit_alias", ("explicit_album_alias",)
    return None


def album_candidates(artist, album, results):
    """Return unique accepted catalog candidates; no search-order tie breakers."""
    accepted = {}
    for item in results:
        if not isinstance(item, dict) or item.get("resultType") != "album":
            continue
        artist_match = _artist_match(artist, item)
        if artist_match is None:
            continue
        url = _album_url(item)
        if not url:
            continue
        found_title = str(item.get("title") or "").strip()
        if _requests_complete_release(album) and not _requests_complete_release(found_title):
            continue
        title_rank = _rank_title(artist, album, found_title)
        if title_rank is None:
            continue
        title_priority, match_type, rules = title_rank
        credit_rank, actual_artist = artist_match
        score = (title_priority, credit_rank)
        candidate = {
            "status": "found", "url": url, "artist": actual_artist,
            "artist_match_type": "ensemble" if credit_rank else "exact",
            "album": found_title, "requested_album": album,
            "match_type": match_type, "normalization_rules": list(rules),
            "year": item.get("year"), "catalog_verified": True,
            "_score": score,
        }
        # A catalog entry returned by multiple searches is one candidate.
        previous = accepted.get(url)
        if previous is None or (score, norm(found_title)) < (previous["_score"], norm(previous["album"])):
            accepted[url] = candidate
    return sorted(accepted.values(), key=lambda c: (
        c["_score"], c["url"], norm(c["album"]),
    ))


def choose_album(artist, album, results):
    candidates = album_candidates(artist, album, results)
    if not candidates:
        return None
    best_score = candidates[0]["_score"]
    best = [candidate for candidate in candidates if candidate["_score"] == best_score]
    if len(best) != 1:
        return {
            "status": "ambiguous",
            "message": "Several equally ranked albums; no automatic download.",
            "artist": artist, "requested_album": album,
            "candidate_count": len(candidates),
            "candidates": [
                {"artist": c["artist"], "album": c["album"], "url": c["url"],
                 "match_type": c["match_type"]}
                for c in best
            ],
        }
    winner = dict(best[0])
    winner.pop("_score")
    winner["candidate_count"] = len(candidates)
    winner["selected_from"] = "all_catalog_results"
    return winner


def pick_exact_album(artist, album, results):
    """Compatibility entrypoint, now with deterministic tie rejection."""
    return choose_album(artist, album, results)


def _queries(artist, album):
    base = base_album_title(album)
    queries = [f"{artist} {album}"]
    if norm(base) != norm(album):
        queries.append(f"{artist} {base}")
    else:
        queries.extend((f"{artist} {base} deluxe", f"{artist} {base} remastered"))
    # Only explicit aliases are allowed to extend the search beyond editions.
    for credit, left, right in _EXPLICIT_ALIASES:
        if norm(credit) == norm(artist):
            if norm(left) == norm(album):
                queries.append(f"{artist} {right}")
            elif norm(right) == norm(album):
                queries.append(f"{artist} {left}")
    if not norm(artist).endswith(tuple(" " + norm(n) for n in _ENSEMBLE_NAMES)):
        queries.extend(f"{artist} {ensemble} {base}" for ensemble in ("Trio", "Quartet", "Quintet"))
    return list(dict.fromkeys(queries))


@lru_cache(maxsize=256)
def _lookup(artist, album):
    from ytmusicapi import YTMusic

    catalog = YTMusic()
    queries = _queries(artist, album)
    results = []
    for query in queries:
        results.extend(catalog.search(query, filter="albums", limit=20) or [])
    outcome = choose_album(artist, album, results)
    if outcome is not None:
        outcome["queries"] = queries
        return outcome
    return {
        "status": "not_found",
        "message": "No album matched the deterministic title and edition rules.",
        "queries": queries,
    }


def lookup_album(artist, album):
    artist = artist.strip()
    album = album.strip()
    if not artist or not album or len(artist) > 200 or len(album) > 250:
        raise ValueError("Enter an artist and album title.")
    return _lookup(artist, album)
