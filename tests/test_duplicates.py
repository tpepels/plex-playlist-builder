"""Whole-album duplicate review, album directories and no-deletion safety tests."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace

from app.duplicates import duplicates_tsv, find_duplicates


def track(key, *, artist="Bill Evans", album="Waltz for Debby",
          title="My Foolish Heart", duration=300000, disc=1, index=1,
          album_id=None, path=None, paths=None):
    paths = paths if paths is not None else ([path] if path else [])
    return SimpleNamespace(
        ratingKey=str(key), parentRatingKey=album_id or ("album-" + str(key)),
        grandparentTitle=artist, parentTitle=album, title=title,
        duration=duration, parentIndex=disc, index=index,
        addedAt=datetime(2026, 10, 26, tzinfo=timezone.utc),
        media=[SimpleNamespace(parts=[SimpleNamespace(file=p)]) for p in paths],
    )


def release(seed, folder, *, album="Waltz for Debby", album_id=None,
            durations=None):
    durations = durations or [300000, 220000, 180000]
    titles = ["My Foolish Heart", "Waltz for Debby", "Detour Ahead"]
    return [
        track(seed + i, album=album, album_id=album_id or f"alb-{seed}",
              title=titles[i], duration=duration, index=i + 1,
              path=f"{folder}/{i + 1:02d}.flac")
        for i, duration in enumerate(durations)
    ]


def test_whole_album_duplicates_group_copies_by_directory_not_song():
    tracks = release(10, "/music/Bill Evans/Waltz for Debby") + release(
        20, "/music/Bill Evans/Waltz for Debby (copy)")
    report = find_duplicates(tracks)
    assert report["summary"]["library_tracks"] == 6
    assert report["summary"]["album_groups"] == 1
    assert report["summary"]["full_album_groups"] == 1
    assert report["summary"]["folder_review_candidates"] == 2
    group = report["groups"][0]
    assert group["album"] == "Waltz for Debby"
    assert group["kind"] == "full"
    assert group["total_copies"] == 2
    assert len(group["releases"]) == 2
    assert {r["directory"] for r in group["releases"]} == {
        "/music/Bill Evans/Waltz for Debby", "/music/Bill Evans/Waltz for Debby (copy)"
    }
    assert all(r["tracks"] == 3 for r in group["releases"])
    assert all(r["folder_review_candidate"] for r in group["releases"])


def test_partial_album_is_not_a_folder_review_candidate():
    tracks = release(1, "/music/Bill Evans/complete") + release(
        10, "/music/Bill Evans/incomplete")[:2]
    report = find_duplicates(tracks)
    assert report["summary"]["partial_groups"] == 1
    assert report["summary"]["folder_review_candidates"] == 0
    assert report["groups"][0]["kind"] == "partial"


def test_remaster_is_an_edition_overlap_not_a_disposable_album():
    tracks = release(1, "/music/Bill Evans/Original", album="You Must Believe in Spring") + release(
        10, "/music/Bill Evans/Remaster", album="You Must Believe in Spring (2022 Remaster)")
    report = find_duplicates(tracks)
    assert report["summary"]["edition_groups"] == 1
    assert report["summary"]["full_album_groups"] == 0
    assert report["summary"]["folder_review_candidates"] == 0
    assert report["groups"][0]["kind"] == "edition"


def test_song_with_same_title_on_other_albums_is_not_a_duplicate():
    report = find_duplicates([
        track(1, album="Portrait in Jazz", title="Autumn Leaves",
              path="/music/Bill Evans/Portrait/01.flac"),
        track(2, album="At the Montreux Jazz Festival", title="Autumn Leaves",
              path="/music/Bill Evans/Montreux/01.flac"),
    ])
    assert report["groups"] == []


def test_different_take_duration_is_not_an_exact_album_copy():
    report = find_duplicates(
        release(1, "/music/Bill Evans/Original") +
        release(10, "/music/Bill Evans/Other", durations=[300000, 225000, 180000])
    )
    assert report["summary"]["full_album_groups"] == 0
    assert report["summary"]["partial_groups"] == 1
    assert report["summary"]["folder_review_candidates"] == 0


def test_media_versions_under_one_plex_track_are_separate_files():
    item = track(
        1, album_id="one", paths=[
            "/music/Bill Evans/Album FLAC/01.flac",
            "/music/Bill Evans/Album MP3/01.mp3",
        ]
    )
    report = find_duplicates([item])
    assert report["summary"]["library_tracks"] == 1
    assert report["summary"]["full_album_groups"] == 1
    assert report["summary"]["folder_review_candidates"] == 2


def test_disc_subdirectories_are_grouped_at_album_root():
    def disc_track(key, folder, disc, index):
        return track(key, album_id="disc-album", disc=disc, index=index,
                     title=f"Disc {disc} Track {index}",
                     path=f"{folder}/Disc {disc}/{index:02d}.flac")
    report = find_duplicates([
        disc_track(1, "/music/Bill Evans/Complete A", 1, 1),
        disc_track(2, "/music/Bill Evans/Complete A", 2, 1),
        disc_track(3, "/music/Bill Evans/Complete B", 1, 1),
        disc_track(4, "/music/Bill Evans/Complete B", 2, 1),
    ])
    assert report["summary"]["full_album_groups"] == 1
    assert report["summary"]["folder_review_candidates"] == 2
    assert all(r["tracks"] == 2 for r in report["groups"][0]["releases"])


def test_shared_artist_directory_cannot_be_marked_for_removal():
    # Two artists/albums appear within one Plex-observed folder, including
    # a sibling not itself part of the duplicate group.
    tracks = release(1, "/music/Jazz/Bill Evans Shared") + release(
        10, "/music/Jazz/Waltz for Debby backup"
    )
    tracks.append(track(40, artist="Miles Davis", album="Kind of Blue",
                        path="/music/Jazz/Bill Evans Shared/other.flac"))
    report = find_duplicates(tracks)
    assert report["summary"]["full_album_groups"] == 1
    assert report["summary"]["folder_review_candidates"] == 1
    records = {r["directory"]: r for r in report["groups"][0]["releases"]}
    assert not records["/music/Jazz/Bill Evans Shared"]["folder_review_candidate"]
    assert "Shared directory" in records["/music/Jazz/Bill Evans Shared"]["folder_status"]


def test_no_media_paths_means_no_directory_recommendations():
    report = find_duplicates([track(1), track(2)])
    assert report["summary"]["full_album_groups"] == 1
    assert report["summary"]["tracks_without_media_path"] == 2
    assert report["summary"]["folder_review_candidates"] == 0
    assert all(r["directory"] == "" for r in report["groups"][0]["releases"])


def test_directory_at_artist_or_music_root_is_not_removable():
    report = find_duplicates([
        track(1, path="/music/Bill Evans/01.flac"),
        track(2, path="/music/Bill Evans/02.flac"),
    ])
    assert report["summary"]["folder_review_candidates"] == 0


def test_duplicate_same_rating_key_is_not_double_counted():
    one = track(1, path="/music/Bill Evans/Original/01.flac")
    report = find_duplicates([one, one, track(2, path="/music/Bill Evans/Backup/01.flac")])
    assert report["summary"]["library_tracks"] == 2
    assert report["summary"]["full_album_groups"] == 1


def test_missing_duration_makes_album_partial_not_safe_to_remove():
    report = find_duplicates([
        track(1, duration=0, path="/music/Bill Evans/Original/01.flac"),
        track(2, duration=0, path="/music/Bill Evans/Backup/01.flac"),
    ])
    assert report["summary"]["folder_review_candidates"] == 0


def test_complete_tsv_has_one_row_per_album_copy_with_directory():
    report = find_duplicates(release(1, "/music/Bill Evans/A") +
                             release(10, "/music/Bill Evans/B"))
    lines = list(csv.DictReader(io.StringIO(duplicates_tsv(report)), delimiter="\t"))
    assert len(lines) == 2
    assert {line["directory"] for line in lines} == {
        "/music/Bill Evans/A", "/music/Bill Evans/B"
    }
    assert all(line["tracks"] == "3" for line in lines)
    assert all(line["folder_review_candidate"] == "True" for line in lines)
    assert all(line["track_rating_keys"] for line in lines)


def test_empty_report_still_has_tsv_headers():
    assert duplicates_tsv(find_duplicates([])).startswith(
        "group\tmatch\tartist\talbum\trelease\tdirectory\t"
    )


def test_album_duplicate_api_uses_cached_library_and_is_read_only(monkeypatch):
    from app import main

    class FakeMusic:
        def searchTracks(self):
            calls.append("scan")
            return release(1, "/music/Bill Evans/A") + release(
                10, "/music/Bill Evans/B")

    calls = []
    monkeypatch.setattr(main, "connect", lambda: (object(), FakeMusic()))
    monkeypatch.setattr(main, "insights_track_cache", {})
    monkeypatch.setattr(main.time, "monotonic", lambda: 1000.0)
    result = main.library_duplicates()
    assert result["summary"]["full_album_groups"] == 1
    response = main.library_duplicates_export()
    assert b"/music/Bill Evans/A" in response.body
    assert response.headers["x-plex-duplicate-groups"] == "1"
    assert calls == ["scan"]
    main.library_duplicates(refresh=True)
    assert calls == ["scan", "scan"]
