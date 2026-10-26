from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.insights import analyze
from app.playlist_policies import read_policies, set_policy
from app.youtube_album import pick_exact_album, lookup_album


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
    assert pick_exact_album("Nils Frahm", "Felt", results[:3]) is None


def test_youtube_album_url_never_guessed_without_valid_id():
    result = pick_exact_album("A", "B", [
        {"resultType": "album", "title": "B", "artist": "A",
         "browseId": "javascript:alert(1)"}
    ])
    assert result is None


def test_youtube_album_empty_input_validation():
    with pytest.raises(ValueError):
        lookup_album(" ", "Felt")
