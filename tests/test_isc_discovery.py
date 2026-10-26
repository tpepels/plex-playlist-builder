"""Source-grounded In Sheep's Clothing discovery tests (no live HTTP)."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from app import isc_discovery as service


LISTING = """<html><body>
<a href="/collection/">Collection</a>
<a href="/album/hiroshi-suzuki-cat/"><img alt="Hiroshi Suzuki – Cat album cover"></a>
<a href="https://insheepsclothinghifi.com/album/lali-puna-scary-world-theory/">
  <span>Lali Puna – Scary World Theory</span></a>
<a href="https://evil.example/album/steal/">Bad Person – Bad Album</a>
<a href="/album/one-artist/">Ambiguous Record</a>
<a href="/product/deluxe">John Coltrane – Blue Train</a>
<a href="/album/lali-puna-scary-world-theory/">Lali Puna – Scary World Theory</a>
</body></html>"""

DETAIL = """<html><h1>Hiroshi Suzuki – Cat</h1>
<div>Style: Japan, Jazz, Funk</div>
<div>Label: Three Blind Mice</div>
<div>Released: 1975</div>
<p>Tags: Jazz-Funk / Modal / Japanese Jazz</p>
<p>Musicians are adventurous, though the prose should NOT become a category.</p>
<h3>Suggested Records</h3><a href="/album/wrong">Other Person – Another Record</a>
</html>"""


def test_listing_reads_only_valid_source_album_cards_and_deduplicates():
    albums = service.parse_collection(LISTING, style="japan")
    assert [(a["artist"], a["album"]) for a in albums] == [
        ("Hiroshi Suzuki", "Cat"), ("Lali Puna", "Scary World Theory")
    ]
    assert all(a["source_url"].startswith("https://insheepsclothinghifi.com/album/")
               for a in albums)


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "http://insheepsclothinghifi.com/album/x/",
    "https://evil.example/album/abc/",
    "https://insheepsclothinghifi.com/collection/",
    "https://insheepsclothinghifi.com/album/../wp-json/",
    "https://evil.example@insheepsclothinghifi.com:444/album/x/",
])
def test_non_album_and_unsafe_urls_are_rejected(url):
    assert service._safe_album_url(url) == ""


def test_detail_uses_explicit_source_taxonomy_only():
    details = service.parse_album_details(DETAIL)
    assert details["year"] == 1975
    assert details["site_styles"] == ["Japan", "Jazz", "Funk"]
    assert "Jazz-Funk" in details["site_tags"]
    record = {
        "source_styles": ["japan"], **details,
    }
    assert "japanese-jazz-funk" in service.categorize(record)


def test_moods_are_conservative_and_dont_claim_every_japanese_album_is_funk():
    record = {"source_styles": ["japan"], "site_styles": [], "site_tags": []}
    assert "japanese-jazz-funk" not in service.categorize(record)
    record["site_styles"] = ["Jazz", "Ambient"]
    categories = service.categorize(record)
    assert "ecm-jazz" in categories
    assert "deep-listening" in categories
    assert "ambient" in categories
    assert "japanese-jazz-funk" not in categories


def test_indietronica_can_match_experimental_pop_only_on_source_tags():
    record = {"source_styles": ["electronic"], "site_styles": ["Rock"],
              "site_tags": ["Downtempo", "Indietronica"]}
    assert "experimental-pop" in service.categorize(record)
    assert "japanese-jazz-funk" not in service.categorize(record)


def test_source_fetch_is_bounded_deduped_and_cached(tmp_path, monkeypatch):
    cache = tmp_path / "isc-discovery.json"
    monkeypatch.setattr(service, "CACHE", cache)
    monkeypatch.setattr(service, "SOURCE_STYLES", ("japan", "jazz"))
    monkeypatch.setattr(service, "DETAIL_LIMIT", 2)
    calls = []
    def fetch(url):
        calls.append(url)
        if "/collection/" in url:
            return LISTING
        assert url.startswith("https://insheepsclothinghifi.com/album/")
        return DETAIL

    first = service.fetch_catalog(fetch_html=fetch)
    assert first["sources_scanned"] == 2
    assert len(first["albums"]) == 2
    assert first["albums"][0]["source_styles"] == ["japan", "jazz"]
    assert first["albums"][0]["details_status"] == "detail"
    assert cache.exists()
    n = len(calls)
    second = service.fetch_catalog(fetch_html=fetch)
    assert second["cached"] is True
    assert len(calls) == n
    third = service.fetch_catalog(refresh=True, fetch_html=fetch)
    assert third["cached"] is False
    assert len(calls) > n


def test_failed_refresh_uses_stale_cache_and_reports_it(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "CACHE", tmp_path / "test.json")
    monkeypatch.setattr(service, "SOURCE_STYLES", ("japan",))
    monkeypatch.setattr(service, "DETAIL_LIMIT", 0)
    service.fetch_catalog(fetch_html=lambda url: LISTING)
    def fail(url):
        raise ValueError("unavailable")
    result = service.fetch_catalog(refresh=True, fetch_html=fail)
    assert result["stale"] is True
    assert result["cached"] is True
    assert result["warnings"]


def test_missing_catalog_is_reported_not_fabricated(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "CACHE", tmp_path / "test.json")
    monkeypatch.setattr(service, "SOURCE_STYLES", ("ambient",))
    with pytest.raises(RuntimeError, match="collection is unavailable"):
        service.fetch_catalog(fetch_html=lambda url: "<html>Unavailable</html>")


def track(key, artist, album, album_key):
    return SimpleNamespace(
        ratingKey=str(key), grandparentTitle=artist, parentTitle=album,
        parentRatingKey=album_key,
    )


def catalog_records(*pairs):
    return {
        "albums": [
            {"artist": a, "album": b, "moods": [],
             "source_url": f"https://insheepsclothinghifi.com/album/release-{i}/",
             "source_styles": ["jazz"], "site_styles": [], "site_tags": []}
            for i, (a, b) in enumerate(pairs)
        ],
        "fetched_at": 1793040000, "sources_scanned": 1,
        "source_name": "In Sheep's Clothing Hi-Fi",
    }


def test_library_matching_respects_artist_title_editions_and_track_count():
    source = catalog_records(
        ("Hiroshi Suzuki", "Cat"),
        ("The Blue Nile", "Hats"),
        ("Enrico Pieranunzi", "The Night Gone By"),
        ("Isao Suzuki", "Blow Up"),
    )
    library = [
        track(1, "Hiroshi Suzuki", "Cat", "cat"),
        track(2, "Hiroshi Suzuki", "Cat", "cat"),
        track(3, "The Blue Nile", "Hats (Deluxe Edition)", "hats"),
        track(4, "Different Artist", "The Night Gone By", "night"),
        track(5, "Isao Suzuki Trio", "Blow Up", "blow"),
    ]
    result = service.compare_library(source, library)
    assert [a["status"] for a in result["albums"]] == [
        "owned", "edition", "missing", "owned"
    ]
    assert result["summary"] == {
        "total": 4, "owned": 2, "edition": 1, "missing": 1,
        "source_categories": []
    }
    assert result["albums"][0]["plex_matches"][0]["tracks"] == 2
    assert result["albums"][1]["plex_matches"][0]["album"] == "Hats (Deluxe Edition)"


def test_album_title_substring_not_accepted_as_owned():
    source = catalog_records(("Bill Evans", "Interplay"))
    result = service.compare_library(source, [
        track(1, "Bill Evans", "Interplay Live at NYC", "wrong"),
        track(2, "Another Artist", "Interplay", "wrong2"),
    ])
    assert result["albums"][0]["status"] == "missing"


def test_api_calls_source_and_plex_without_playlist_mutations(monkeypatch):
    from app import main
    source = catalog_records(("Hiroshi Suzuki", "Cat"))
    calls = []
    monkeypatch.setattr(main, "fetch_catalog", lambda refresh: (calls.append(("fetch", refresh)), source)[1])
    monkeypatch.setattr(main, "connect", lambda: (object(), object()))
    monkeypatch.setattr(main, "_insights_tracks",
                        lambda music, refresh: (calls.append(("plex", refresh)),
                                                [track(1, "Hiroshi Suzuki", "Cat", "cat")])[1])
    result = main.discover_isc(refresh=True)
    assert result["summary"]["owned"] == 1
    assert calls == [("fetch", True), ("plex", True)]
