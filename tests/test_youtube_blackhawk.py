"""Blackhawk complete-album release matching without collapsing two nights."""
from __future__ import annotations

import sys
from types import SimpleNamespace

from app import youtube_album as resolver


TITLE = "In Person Friday And Saturday Nights At The Blackhawk"
COMPLETE = TITLE + ", Complete"


def candidate(title, *, artist="Miles Davis", browse="MPREblackhawk"):
    return {"resultType": "album", "title": title, "artist": artist, "browseId": browse}


def test_colon_is_already_ignored_in_title_normalization():
    assert resolver.norm("In Person: Friday And Saturday Nights At The Blackhawk") == (
        resolver.norm(TITLE)
    )


def test_recognizes_complete_with_comma_dash_or_parentheses():
    for title in (COMPLETE, TITLE + " - Complete", TITLE + " (Complete Edition)"):
        assert resolver.base_album_title(title) == TITLE
    assert resolver.parenthetical_base_album_title(TITLE + " - Complete (Live)") == TITLE + " - Complete (Live)"
    assert resolver.base_album_title(TITLE + ", Volume 2") == TITLE + ", Volume 2"


def test_complete_blackhawk_matches_same_two_nights_with_live_suffix():
    album = candidate(TITLE + " - Complete (Live)")
    result = resolver.pick_exact_album("Miles Davis", COMPLETE, [album])
    assert result is not None
    assert result["album"] == album["title"]
    assert result["match_type"] == "explicit_alias"
    assert result["url"] == "https://music.youtube.com/browse/MPREblackhawk"


def test_requested_complete_never_selects_a_single_night_or_short_release():
    candidates = [
        candidate("In Person: Friday Night At The Blackhawk, Complete"),
        candidate("In Person Saturday Night At The Blackhawk, Complete"),
        candidate(TITLE),
        candidate(COMPLETE, artist="Other Musician"),
    ]
    assert resolver.pick_exact_album("Miles Davis", COMPLETE, candidates) is None


def test_specific_complete_release_takes_precedence_over_other_editions():
    candidates = [
        candidate(TITLE + " - Complete (Live)", browse="MPRElive"),
        candidate(COMPLETE, browse="MPREexact"),
    ]
    result = resolver.pick_exact_album("Miles Davis", COMPLETE, candidates)
    assert result["match_type"] == "exact"
    assert result["url"].endswith("MPREexact")


def test_query_retries_without_complete_when_first_search_has_no_match(monkeypatch):
    queries = []

    def search(query, filter, limit):
        queries.append(query)
        if query == f"Miles Davis {TITLE}":
            return [candidate(TITLE + " - Complete (Live)")]
        return []

    monkeypatch.setitem(
        sys.modules, "ytmusicapi",
        SimpleNamespace(YTMusic=lambda: SimpleNamespace(search=search)),
    )
    resolver._lookup.cache_clear()
    try:
        result = resolver.lookup_album("Miles Davis", COMPLETE)
        assert result["status"] == "found"
        assert result["album"] == TITLE + " - Complete (Live)"
        assert queries[:2] == [f"Miles Davis {COMPLETE}", f"Miles Davis {TITLE}"]
    finally:
        resolver._lookup.cache_clear()
