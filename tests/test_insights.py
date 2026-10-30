from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.insights import analyze
from app.playlist_policies import read_policies, set_policy
from app.youtube_album import base_album_title, parenthetical_base_album_title, pick_exact_album, lookup_album


NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)


def t(key, artist, album, song, age, album_key=None):
    return SimpleNamespace(
        ratingKey=key, parentRatingKey=album_key or f"{artist}-{album}",
        grandparentTitle=artist, parentTitle=album, title=song,
        addedAt=NOW-timedelta(days=age), parentIndex=1, index=int(key),
    )


def test_insights_are_based_on_unique_keys_and_expose_missing_references():
    tracks = [t(1, "Artist A", "One", "First", 1),
              t(2, "Artist A", "One", "Second", 20),
              t(3, "Artist B", "Two", "Third", 40),
              t(4, "Artist C", "Three", "Fourth", 4)]
    playlists = {
        "P1": {"items": [{"rating_key": 1}, {"rating_key": 1}, {"rating_key": 2}, {"rating_key": 999}]},
        "P2": {"items": [{"rating_key": 1}, {"rating_key": 3}]},
    }
    report = analyze(tracks, playlists, now=NOW, recent_days=30)
    s = report["summary"]
    assert s["library_tracks"] == 4
    assert s["listed_tracks"] == 3
    assert s["unlisted_tracks"] == 1
    assert s["in_multiple_playlists"] == 1
    assert s["repeat_placements"] == 1
    assert s["missing_references"] == 1
    first = report["playlists"][0]
    assert first["tracks"] == 2
    assert first["repeated_tracks"] == 1
    assert first["missing_references"] == 1
    assert first["recent_tracks"] == 2  # both tracks were added within the 30-day window
    assert first["shared_tracks"] == 1
    assert first["shared_share"] == 0.5
    assert first["flags"] == ["Missing library matches", "Repeated tracks"]
    # Curated playlists are observed but never judged by concentration targets.
    assert "Artist concentration" not in first["flags"]


def test_album_coverage_marks_only_new_partly_covered_albums():
    tracks = [t(1, "One", "Covered", "x", 2),
              t(2, "One", "Covered", "y", 2),
              t(3, "Other", "Uncovered", "a", 1),
              t(4, "Other", "Uncovered", "b", 1),
              t(5, "Old", "Old record", "c", 300)]
    report = analyze(tracks, {"P": {"items": [{"rating_key": 1}, {"rating_key": 2}]}}, now=NOW)
    albums = report["recent_albums"]
    assert [a["album"] for a in albums] == ["Uncovered", "Covered"]
    assert albums[0]["needs_attention"] is True
    assert albums[0]["covered_tracks"] == 0
    assert albums[1]["needs_attention"] is False
    assert albums[1]["coverage_share"] == 1
    assert len(albums[0]["track_items"]) == 2
    assert report["summary"]["uncurated_recent_albums"] == 1


def test_rotating_thresholds_flag_only_rotating_playlists():
    tracks = [t(i, "Same artist" if i <= 14 else "Artist "+str(i),
                "Album" if i <= 14 else "Other "+str(i),
                "Track "+str(i), 90, album_key="album-one" if i <= 14 else "album-"+str(i))
              for i in range(1, 21)]
    lists = {"Focused": {"items": [{"rating_key": i} for i in range(1, 21)]}}
    curated = analyze(tracks, lists, now=NOW)["playlists"][0]
    rotating = analyze(tracks, lists, policies={"Focused": "rotating"}, now=NOW)["playlists"][0]
    assert not curated["flags"]
    assert set(rotating["flags"]) == {"Artist concentration", "Album concentration", "Needs discoveries"}
    assert rotating["kind"] == "rotating"


def test_policy_toggle_is_persistent_without_changing_playlist_definitions(tmp_path, monkeypatch):
    import app.playlist_policies as store
    monkeypatch.setattr(store, "POLICIES_FILE", tmp_path/"playlist-policies.yml")
    assert read_policies() == {}
    set_policy("Favorite", "rotating", ["Favorite"])
    assert read_policies() == {"Favorite": "rotating"}
    set_policy("Favorite", "curated", ["Favorite"])
    assert read_policies() == {}
    with pytest.raises(ValueError):
        set_policy("Unknown", "rotating", ["Favorite"])


def test_youtube_album_resolver_only_returns_exact_official_album_links():
    results = [
        {"resultType": "song", "title": "Felt", "artist": "Nils Frahm", "browseId": "MPREwrong"},
        {"resultType": "album", "title": "Felt", "artist": "Another", "playlistId": "OLAK5uy_other"},
        {"resultType": "album", "title": "Felt (Remastered)", "artist": "Nils Frahm", "playlistId": "OLAK5uy_edition"},
        {"resultType": "album", "title": "Felt", "artist": "Nils Frahm",
         "playlistId": "OLAK5uy_correctID", "browseId": "MPREvalid"},
    ]
    link = pick_exact_album("Nils Frahm", "Felt", results)
    assert link["url"] == "https://music.youtube.com/playlist?list=OLAK5uy_correctID"
    assert link["catalog_verified"] is True
    assert pick_exact_album("Nils Frahm", "Not Felt", results) is None
    variant = pick_exact_album("Nils Frahm", "Felt", results[:3])
    assert variant["match_type"] == "edition"
    assert variant["album"] == "Felt (Remastered)"


def test_youtube_album_url_never_guessed_without_valid_id():
    result = pick_exact_album("A", "B", [
        {"resultType": "album", "title": "B", "artist": "A",
         "browseId": "javascript:alert(1)"}
    ])
    assert result is None


def test_youtube_album_empty_input_validation():
    with pytest.raises(ValueError):
        lookup_album(" ", "Felt")



@pytest.mark.parametrize("title,expected", [
    ("Hats (Deluxe Edition)", "Hats"),
    ("Hats [Deluxe Version]", "Hats"),
    ("Hats (2002 Remaster)", "Hats"),
    ("Hats (Remastered 2011)", "Hats"),
    ("Hats - 2013 Remaster", "Hats"),
    ("Hats – Expanded Edition", "Hats"),
    ("Hats (40th Anniversary Edition)", "Hats"),
    ("Hats (Deluxe Edition) [2020 Remaster]", "Hats"),
    ("Hats", "Hats"),
    ("Hats (Live)", "Hats (Live)"),
    ("Hats (Acoustic)", "Hats (Acoustic)"),
    ("Hats (Vol. 2)", "Hats (Vol. 2)"),
    ("Hats (Demo)", "Hats (Demo)"),
    ("The Complete Edition", "The Complete Edition"),
])
def test_youtube_album_known_edition_tags_only(title, expected):
    assert base_album_title(title) == expected


def test_youtube_resolver_matches_deluxe_and_remaster_in_both_directions():
    records = [
        {"resultType": "album", "title": "Hats (Deluxe Edition)",
         "artist": "The Blue Nile", "browseId": "MPREdeluxe"},
        {"resultType": "album", "title": "Hats (2002 Remaster)",
         "artist": "The Blue Nile", "playlistId": "OLAK5uy_remaster"},
    ]
    match = pick_exact_album("The Blue Nile", "Hats", records)
    assert match["status"] == "found"
    assert match["match_type"] == "edition"
    assert match["url"].startswith("https://music.youtube.com/")
    assert match["requested_album"] == "Hats"

    specific = pick_exact_album("The Blue Nile", "Hats (2002 Remaster)", records)
    assert specific["match_type"] == "exact"
    assert specific["album"] == "Hats (2002 Remaster)"
    regular = pick_exact_album("The Blue Nile", "Hats (2002 Remaster)", [
        {"resultType": "album", "title": "Hats",
         "artist": "The Blue Nile", "browseId": "MPREstandard"}
    ])
    assert regular["match_type"] == "edition"


def test_youtube_resolver_prefers_exact_standard_album_over_other_editions():
    records = [
        {"resultType": "album", "title": "Hats (Deluxe Edition)",
         "artist": "The Blue Nile", "browseId": "MPREdeluxe"},
        {"resultType": "album", "title": "Hats",
         "artist": "The Blue Nile", "browseId": "MPREstandard"},
    ]
    result = pick_exact_album("The Blue Nile", "Hats", records)
    assert result["album"] == "Hats"
    assert result["match_type"] == "exact"


def test_youtube_resolver_preserves_album_identity_and_safe_urls():
    records = [
        {"resultType": "album", "title": "Hats (Deluxe Edition)",
         "artist": "Another Artist", "browseId": "MPREwrongartist"},
        {"resultType": "album", "title": "Hats (Live)",
         "artist": "The Blue Nile", "browseId": "MPRElive"},
        {"resultType": "album", "title": "Hats: The Live Sessions",
         "artist": "The Blue Nile", "browseId": "MPREsessions"},
        {"resultType": "song", "title": "Hats",
         "artist": "The Blue Nile", "browseId": "MPREsong"},
        {"resultType": "album", "title": "Hats [2011 Remaster]",
         "artist": "The Blue Nile", "playlistId": "https://evil.example"},
    ]
    variant = pick_exact_album("The Blue Nile", "Hats", records)
    assert variant is None  # Live is a distinct recording, not an arbitrary edition
    assert pick_exact_album("The Blue Nile", "Hats (Live)", records)["match_type"] == "exact"


def test_youtube_resolver_supports_artist_list_from_catalog():
    result = pick_exact_album("Arve Henriksen", "Places of Worship", [
        {"resultType": "album", "title": "Places of Worship (Remastered)",
         "artists": [{"name": "Arve Henriksen"}], "browseId": "MPREreal"},
    ])
    assert result["match_type"] == "edition"


def test_youtube_lookup_broadens_queries_after_no_match(monkeypatch):
    import sys
    from types import SimpleNamespace
    from app import youtube_album as service

    queries = []
    def search(query, filter, limit):
        queries.append((query, filter, limit))
        if query == "The Blue Nile Hats deluxe":
            return [{"resultType": "album", "title": "Hats (Deluxe Edition)",
                     "artist": "The Blue Nile", "browseId": "MPREreal"}]
        return []

    monkeypatch.setitem(sys.modules, "ytmusicapi",
                        SimpleNamespace(YTMusic=lambda: SimpleNamespace(search=search)))
    service._lookup.cache_clear()
    try:
        result = service.lookup_album("The Blue Nile", "Hats")
        assert result["status"] == "found"
        assert result["match_type"] == "edition"
        assert [q[0] for q in queries][:2] == [
            "The Blue Nile Hats", "The Blue Nile Hats deluxe"
        ]
    finally:
        service._lookup.cache_clear()


def test_youtube_lookup_retries_without_requested_edition(monkeypatch):
    import sys
    from types import SimpleNamespace
    from app import youtube_album as service

    queries = []
    def search(query, filter, limit):
        queries.append(query)
        if query == "The Blue Nile Hats":
            return [{"resultType": "album", "title": "Hats",
                     "artist": "The Blue Nile", "browseId": "MPREreal"}]
        return []

    monkeypatch.setitem(sys.modules, "ytmusicapi",
                        SimpleNamespace(YTMusic=lambda: SimpleNamespace(search=search)))
    service._lookup.cache_clear()
    try:
        result = service.lookup_album("The Blue Nile", "Hats (2002 Remaster)")
        assert result["status"] == "found"
        assert result["match_type"] == "edition"
        assert queries[:2] == [
            "The Blue Nile Hats (2002 Remaster)", "The Blue Nile Hats"
        ]
    finally:
        service._lookup.cache_clear()


@pytest.mark.parametrize("credited", [
    "Isao Suzuki Trio",
    "Isao Suzuki Quartet",
    "Isao Suzuki Trio & Isao Suzuki Quartet",
    "Isao Suzuki Trio/Quartet",
    "Isao Suzuki Trio / Quartet",
])
def test_youtube_resolver_accepts_same_leader_ensemble_credits(credited):
    album = {"resultType": "album", "title": "Blow Up",
             "artist": credited, "browseId": "MPREsuzuki"}
    result = pick_exact_album("Isao Suzuki", "Blow Up", [album])
    assert result["status"] == "found"
    assert result["artist_match_type"] == "ensemble"
    assert result["album"] == "Blow Up"
    assert result["url"] == "https://music.youtube.com/browse/MPREsuzuki"


def test_youtube_resolver_accepts_multiple_ensemble_artist_entities():
    album = {"resultType": "album", "title": "BLOW UP",
             "artists": [{"name": "Isao Suzuki Trio"},
                         {"name": "Isao Suzuki Quartet"}],
             "playlistId": "OLAK5uy_suzuki"}
    result = pick_exact_album("Isao Suzuki", "Blow Up", [album])
    assert result["artist_match_type"] == "ensemble"
    assert result["match_type"] == "exact"


@pytest.mark.parametrize("credited", [
    "Isao Suzuki Bandits", "Isao Suzuki Trio & Another Musician",
    "Another Musician & Isao Suzuki Quartet", "Isao Suzuki Trio Featuring Guest",
    "Isao Suzuki Trio All-Stars", "Isao Suzuki Orchestra of Europe",
    "Isao Suzuki Quartet & Someone Else",
])
def test_youtube_resolver_rejects_ambiguous_or_other_artists(credited):
    album = {"resultType": "album", "title": "Blow Up",
             "artist": credited, "browseId": "MPREwrong"}
    assert pick_exact_album("Isao Suzuki", "Blow Up", [album]) is None


def test_youtube_resolver_rejects_collaboration_in_artist_list():
    album = {"resultType": "album", "title": "Blow Up",
             "artists": [{"name": "Isao Suzuki Trio"},
                         {"name": "Other Artist"}],
             "browseId": "MPREwrong"}
    assert pick_exact_album("Isao Suzuki", "Blow Up", [album]) is None


def test_youtube_resolver_prefers_exact_artist_to_ensemble_credits():
    results = [
        {"resultType": "album", "title": "Blow Up",
         "artist": "Isao Suzuki Quartet", "browseId": "MPREquartet"},
        {"resultType": "album", "title": "Blow Up",
         "artist": "Isao Suzuki", "browseId": "MPREsolo"},
    ]
    resolved = pick_exact_album("Isao Suzuki", "Blow Up", results)
    assert resolved["artist"] == "Isao Suzuki"
    assert resolved["artist_match_type"] == "exact"
    assert resolved["url"].endswith("MPREsolo")


def test_youtube_lookup_searches_alternative_ensemble_credits(monkeypatch):
    import sys
    from types import SimpleNamespace
    from app import youtube_album as service

    queries = []
    def search(query, filter, limit):
        queries.append(query)
        if query == "Isao Suzuki Quartet Blow Up":
            return [{"resultType": "album", "title": "BLOW UP",
                     "artist": "Isao Suzuki Trio & Isao Suzuki Quartet",
                     "browseId": "MPREsuzuki"}]
        return []

    monkeypatch.setitem(sys.modules, "ytmusicapi",
                        SimpleNamespace(YTMusic=lambda: SimpleNamespace(search=search)))
    service._lookup.cache_clear()
    try:
        result = service.lookup_album("Isao Suzuki", "Blow Up")
        assert result["status"] == "found"
        assert result["artist_match_type"] == "ensemble"
        assert queries[:5] == [
            "Isao Suzuki Blow Up", "Isao Suzuki Blow Up deluxe",
            "Isao Suzuki Blow Up remastered",
            "Isao Suzuki Trio Blow Up", "Isao Suzuki Quartet Blow Up",
        ]
    finally:
        service._lookup.cache_clear()


@pytest.mark.parametrize("title,expected", [
    ("Getz At The Gate (Live)", "Getz At The Gate (Live)"),
    ("Getz At The Gate (Recorded 1961)", "Getz At The Gate (Recorded 1961)"),
    ("Getz At The Gate (Live) (2020 Remaster)", "Getz At The Gate (Live)"),
    ("Getz At The Gate (Vol. 2)", "Getz At The Gate (Vol. 2)"),
    ("Getz (Live) At The Gate", "Getz (Live) At The Gate"),
    ("Getz At The Gate", "Getz At The Gate"),
])
def test_parenthetical_base_album_title(title, expected):
    assert parenthetical_base_album_title(title) == expected


def test_youtube_resolver_matches_getz_at_the_gate_live_album():
    catalog = [
        {"resultType": "album", "title": "Getz At The Gate (Live)",
         "artist": "Stan Getz", "browseId": "MPREb_l3o4MNZF1rh"},
    ]
    result = pick_exact_album("Stan Getz", "Getz at the Gate", catalog)
    assert result["status"] == "found"
    assert result["match_type"] == "explicit_alias"
    assert result["url"] == "https://music.youtube.com/browse/MPREb_l3o4MNZF1rh"
    reverse = pick_exact_album("Stan Getz", "Getz at the Gate (Live)", [
        {"resultType": "album", "title": "Getz at the Gate",
         "artist": "Stan Getz", "browseId": "MPREbase"},
    ])
    assert reverse["match_type"] == "explicit_alias"
    assert reverse["url"].endswith("MPREbase")


def test_explicit_alias_matching_never_beats_exact_and_known_editions():
    catalog = [
        {"resultType": "album", "title": "Hats (Live)",
         "artist": "The Blue Nile", "browseId": "MPRElive"},
        {"resultType": "album", "title": "Hats (Remastered)",
         "artist": "The Blue Nile", "browseId": "MPREedition"},
        {"resultType": "album", "title": "Hats",
         "artist": "The Blue Nile", "browseId": "MPREexact"},
    ]
    assert pick_exact_album("The Blue Nile", "Hats", catalog)["album"] == "Hats"
    assert pick_exact_album("The Blue Nile", "Hats", catalog[:-1])["album"] == "Hats (Remastered)"


def test_parenthetical_fallback_still_rejects_nontrailing_or_unrelated_titles():
    catalog = [
        {"resultType": "album", "title": "Getz (Live) At The Gate",
         "artist": "Stan Getz", "browseId": "MPREembedded"},
        {"resultType": "album", "title": "Getz at the Gate (Live)",
         "artist": "Someone Else", "browseId": "MPREother"},
        {"resultType": "album", "title": "Getz at the Gate: New Sessions",
         "artist": "Stan Getz", "browseId": "MPREunrelated"},
    ]
    assert pick_exact_album("Stan Getz", "Getz at the Gate", catalog) is None


def test_youtube_lookup_retries_without_parenthetical_suffix(monkeypatch):
    import sys
    from types import SimpleNamespace
    from app import youtube_album as service

    queries = []
    def search(query, filter, limit):
        queries.append(query)
        if query == "Stan Getz Getz At The Gate":
            return [{"resultType": "album", "title": "Getz At The Gate",
                     "artist": "Stan Getz", "browseId": "MPREb_l3o4MNZF1rh"}]
        return []

    monkeypatch.setitem(sys.modules, "ytmusicapi",
                        SimpleNamespace(YTMusic=lambda: SimpleNamespace(search=search)))
    service._lookup.cache_clear()
    try:
        found = service.lookup_album("Stan Getz", "Getz At The Gate (Live)")
        assert found["status"] == "found"
        assert found["match_type"] == "explicit_alias"
        assert queries[:2] == [
            "Stan Getz Getz At The Gate (Live)",
            "Stan Getz Getz at the Gate",
        ]
    finally:
        service._lookup.cache_clear()
