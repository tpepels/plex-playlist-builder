"""Conservative library duplicate detection and download tests."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace

from app.duplicates import duplicates_tsv, find_duplicates


def track(key, artist="Bill Evans", album="Waltz for Debby", title="My Foolish Heart",
          duration=300000, disc=1, index=1):
    return SimpleNamespace(
        ratingKey=str(key), grandparentTitle=artist, parentTitle=album,
        title=title, duration=duration, parentIndex=disc, index=index,
        addedAt=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )


def test_same_album_same_track_position_and_close_duration_is_likely_duplicate():
    result = find_duplicates([
        track(1, duration=300000), track(2, duration=301400),
        track(3, duration=308000),
    ])
    assert result["summary"]["library_tracks"] == 3
    assert result["summary"]["likely_groups"] == 1
    assert result["summary"]["likely_extra_entries"] == 1
    assert result["summary"]["edition_groups"] == 0
    group = result["groups"][0]
    assert group["kind"] == "likely"
    assert [e["rating_key"] for e in group["entries"]] == ["1", "2"]
    assert group["duration_spread_ms"] == 1400


def test_both_duplicate_edition_names_are_kept_as_review_not_certain_duplicates():
    result = find_duplicates([
        track(1, album="You Must Believe in Spring", title="B Minor Waltz", duration=210000),
        track(2, album="You Must Believe in Spring (2022 Remaster)",
              title="B Minor Waltz", duration=210900),
    ])
    assert result["summary"]["likely_groups"] == 0
    assert result["summary"]["edition_groups"] == 1
    assert result["summary"]["edition_entries_to_review"] == 2
    assert result["groups"][0]["kind"] == "edition"


def test_different_albums_are_not_falsely_flagged_even_for_same_song_length():
    result = find_duplicates([
        track(1, album="Waltz for Debby", title="Autumn Leaves"),
        track(2, album="Portrait in Jazz", title="Autumn Leaves"),
        track(3, album="At the Montreux Jazz Festival", title="Autumn Leaves"),
    ])
    assert result["groups"] == []


def test_different_positions_and_live_takes_are_not_auto_classified():
    result = find_duplicates([
        track(1, index=2),
        track(2, index=5),
        track(3, album="Waltz for Debby (Live)", index=2),
    ])
    assert result["groups"] == []


def test_different_artist_or_duration_does_not_match():
    result = find_duplicates([
        track(1, artist="Bill Evans"),
        track(2, artist="Another Evans"),
        track(3, artist="Bill Evans", duration=310000),
    ])
    assert result["groups"] == []


def test_missing_duration_or_metadata_are_skipped_not_guessed():
    result = find_duplicates([
        track(1, duration=None),
        track(2, duration=0),
        track(3, title=""),
        track(4, duration=300000),
    ])
    assert result["summary"]["library_tracks"] == 3
    assert result["summary"]["missing_duration"] == 2
    assert result["groups"] == []


def test_same_plex_rating_key_is_counted_once():
    original = track(1)
    result = find_duplicates([original, original, track(2)])
    assert result["summary"]["library_tracks"] == 2
    assert result["summary"]["likely_extra_entries"] == 1


def test_duration_spread_is_not_transitively_extended():
    result = find_duplicates([
        track(1, duration=300000), track(2, duration=301800),
        track(3, duration=303400),
    ])
    assert result["summary"]["likely_groups"] == 1
    assert len(result["groups"][0]["entries"]) == 2


def test_duplicate_output_order_and_tsv_export_have_all_entries():
    result = find_duplicates([
        track(9), track(8),
        track(5, album="Waltz for Debby (Deluxe Edition)"),
        track(4, album="Waltz for Debby (2002 Remaster)"),
    ])
    assert [g["kind"] for g in result["groups"]] == ["likely", "edition"]
    reader = list(csv.DictReader(io.StringIO(duplicates_tsv(result)), delimiter="\t"))
    assert {row["rating_key"] for row in reader} == {"4", "5", "8", "9"}
    # A Plex item can legitimately appear in both the same-album group and
    # the wider cross-edition group; the TSV preserves all associations.
    assert [row["group"] for row in reader] == ["1", "1", "2", "2", "2", "2"]
    assert all(row["artist"] == "Bill Evans" for row in reader)


def test_empty_report_still_produces_tsv_header():
    result = find_duplicates([])
    assert result["groups"] == []
    assert duplicates_tsv(result).startswith("group\tkind\tartist\t")


def test_duplicate_api_uses_read_only_cached_library_and_exports(monkeypatch):
    from app import main

    class FakeMusic:
        def searchTracks(self):
            calls.append("scan")
            return [track(1), track(2)]

    calls = []
    music = FakeMusic()
    monkeypatch.setattr(main, "connect", lambda: (object(), music))
    monkeypatch.setattr(main, "insights_track_cache", {})
    monkeypatch.setattr(main.time, "monotonic", lambda: 1000.0)
    result = main.library_duplicates()
    assert result["summary"]["likely_groups"] == 1
    response = main.library_duplicates_export()
    assert b"Bill Evans" in response.body
    assert response.headers["x-plex-duplicate-groups"] == "1"
    assert calls == ["scan"]
    main.library_duplicates(refresh=True)
    assert calls == ["scan", "scan"]
