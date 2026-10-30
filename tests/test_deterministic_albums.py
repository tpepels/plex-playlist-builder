"""Deterministic catalog equivalences, ranking and audit regressions."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from app import youtube_album as albums


def entry(title, key, artist="Miles Davis"):
    return {"resultType": "album", "title": title, "artist": artist, "browseId": key}


def test_only_explicit_spelling_equivalences():
    assert albums.norm("Friday & Saturday, Vol. 2: Pt. 1") == albums.norm(
        "Friday and Saturday Vol 2 Part 1")
    assert albums.norm("The Blue Nile — HATS!") == albums.norm("The Blue Nile Hats")
    assert albums.norm("Vol. 1") != albums.norm("Vol. 2")
    assert albums.norm("Friday") != albums.norm("Saturday")


def test_no_generic_parenthetical_removal():
    for suffix in ("Live", "Demo", "Acoustic", "Mono", "Stereo", "Vol. 2"):
        catalog = [entry("Kind of Blue (" + suffix + ")", "MPRE" + suffix)]
        assert albums.pick_exact_album("Miles Davis", "Kind of Blue", catalog) is None
    assert albums.parenthetical_base_album_title("Kind of Blue (Live)") == "Kind of Blue (Live)"


def test_exact_beats_edition_even_when_exact_is_last():
    catalog = [
        entry("In a Silent Way (Remastered)", "MPREedition"),
        entry("In a Silent Way", "MPREexact"),
    ]
    result = albums.pick_exact_album("Miles Davis", "In a Silent Way", catalog)
    assert result["status"] == "found"
    assert result["url"].endswith("MPREexact")
    assert result["match_type"] == "exact"


def test_equally_ranked_different_album_ids_are_ambiguous():
    catalog = [
        entry("Miles at Newport", "MPREone"),
        entry("Miles At Newport", "MPREtwo"),
    ]
    result = albums.pick_exact_album("Miles Davis", "Miles at Newport", catalog)
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    assert all("url" in item for item in result["candidates"])


def test_same_album_in_multiple_searches_is_deduplicated():
    candidate = entry("Kind of Blue", "MPREsame")
    result = albums.pick_exact_album("Miles Davis", "Kind of Blue", [candidate] * 4)
    assert result["status"] == "found"
    assert result["candidate_count"] == 1
    assert result["normalization_rules"] == ["case_punctuation"]


def test_global_search_does_not_stop_at_earlier_edition(monkeypatch):
    queries = []
    def search(query, filter, limit):
        queries.append(query)
        if query == "Miles Davis Kind of Blue":
            return [entry("Kind of Blue (Remastered)", "MPREedition")]
        if query == "Miles Davis Kind of Blue deluxe":
            return [entry("Kind Of Blue", "MPREexact")]
        return []
    monkeypatch.setitem(sys.modules, "ytmusicapi",
                        SimpleNamespace(YTMusic=lambda: SimpleNamespace(search=search)))
    albums._lookup.cache_clear()
    try:
        result = albums.lookup_album("Miles Davis", "Kind of Blue")
        assert result["url"].endswith("MPREexact")
        assert len(queries) > 1
        assert result["selected_from"] == "all_catalog_results"
        assert result["queries"] == queries
    finally:
        albums._lookup.cache_clear()


def test_complete_cannot_fall_back_to_incomplete():
    catalog = [entry("Miles Live at Newport", "MPREshort")]
    assert albums.pick_exact_album("Miles Davis", "Miles Live at Newport, Complete", catalog) is None


def test_different_ensemble_or_artist_does_not_match():
    catalog = [entry("Kind of Blue", "MPREother", artist="Miles Davis and John Coltrane")]
    assert albums.pick_exact_album("Miles Davis", "Kind of Blue", catalog) is None
