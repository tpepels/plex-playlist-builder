"""Disk-first matching tests: match paths, never guessed classical tags."""
from types import SimpleNamespace as Item

from app.disk_reconciliation import reconcile_inventory, lookup_file, plex_media_index


def music_track(path, artist="Unrelated pianist", album="Other catalogue title"):
    return Item(
        grandparentTitle=artist, parentTitle=album, title="Movement 1",
        ratingKey="1234", media=[Item(parts=[Item(file=path)])],
    )


def inventory():
    return {
        "complete": True, "errors": [], "root": "/data/music",
        "total_audio_files": 3, "album_folders": 2,
        "staging": {"complete": True, "errors": [], "total_audio_files": 0,
                    "stages": []},
        "albums": [
            {
                "folder": "YT-DLP Imports/Pianist/Beethoven Sonatas",
                "artist_folder": "Pianist", "album_folder": "Beethoven Sonatas",
                "imported": True, "size_bytes": 100,
                "files": [
                    "YT-DLP Imports/Pianist/Beethoven Sonatas/01.opus",
                    "YT-DLP Imports/Pianist/Beethoven Sonatas/02.opus",
                ],
            },
            {
                "folder": "Other/Recording", "artist_folder": "Other",
                "album_folder": "Recording", "imported": False,
                "size_bytes": 50, "files": ["Other/Recording/03.mp3"],
            },
        ],
    }


def test_scanned_beethoven_files_are_found_under_different_plex_artist_and_mount():
    report = reconcile_inventory(inventory(), [
        music_track("/plex/music/YT-DLP Imports/Pianist/Beethoven Sonatas/01.opus"),
        music_track("/plex/music/YT-DLP Imports/Pianist/Beethoven Sonatas/02.opus"),
    ], query="Beethoven")
    assert report["total_matches"] == 1
    row = report["items"][0]
    assert row["status"] == "indexed"
    assert row["files_indexed"] == 2
    assert len(row["plex_labels"]) == 1
    assert row["plex_labels"][0]["artist"] == "Unrelated pianist"


def test_existing_files_missing_in_plex_are_reported_without_any_download_job():
    report = reconcile_inventory(inventory(), [], status="missing")
    assert report["disk_audio_files"] == 3
    assert report["disk_status_counts"] == {"missing": 2}
    assert report["total_matches"] == 2
    assert report["items"][0]["files_on_disk"] == 2
    assert report["items"][0]["files_indexed"] == 0


def test_identical_artist_album_tags_never_count_as_matching_if_file_is_different():
    tracks = [music_track("/different/recording/01.opus", artist="Pianist",
                          album="Beethoven Sonatas")]
    result = reconcile_inventory(inventory(), tracks, query="Beethoven")
    assert result["items"][0]["status"] == "missing"


def test_ambiguous_duplicate_mounts_do_not_falsely_claim_indexing():
    tracks = [
        music_track("/plex1/YT-DLP Imports/Pianist/Beethoven Sonatas/01.opus"),
        music_track("/plex2/YT-DLP Imports/Pianist/Beethoven Sonatas/01.opus"),
    ]
    result = reconcile_inventory(inventory(), tracks, query="Beethoven")
    assert result["items"][0]["status"] == "ambiguous"


def test_pagination_is_applied_after_global_scanning_and_status_filter():
    report = reconcile_inventory(inventory(), [], offset=1, limit=1)
    assert report["disk_album_folders"] == 2
    assert report["total_matches"] == 2
    assert len(report["items"]) == 1


def test_incomplete_scan_is_obvious_to_client():
    incomplete = inventory()
    incomplete["complete"] = False
    incomplete["errors"] = ["Audio file safety limit reached."]
    report = reconcile_inventory(incomplete, [])
    assert report["disk_scan_complete"] is False
    assert report["scan_errors"]


def test_primary_unindexed_view_shows_only_physical_files_missing_from_plex():
    # One fully indexed album should disappear from the default results.
    tracks = [music_track("/plex/music/Other/Recording/03.mp3")]
    report = reconcile_inventory(inventory(), tracks, status="unindexed")
    assert report["disk_album_folders"] == 2
    assert report["total_matches"] == 1
    assert report["items"][0]["folder"] == "YT-DLP Imports/Pianist/Beethoven Sonatas"
    assert report["items"][0]["files_not_in_plex"] == 2
    assert report["not_in_plex_audio_files"] == 2
    assert report["not_in_plex_album_folders"] == 1


def test_partially_indexed_albums_appear_in_primary_unindexed_view():
    track = music_track("/plex/music/YT-DLP Imports/Pianist/Beethoven Sonatas/01.opus")
    report = reconcile_inventory(inventory(), [track], status="unindexed")
    assert report["total_matches"] == 2
    beethoven = next(i for i in report["items"] if "Beethoven" in i["folder"])
    assert beethoven["status"] == "partial"
    assert beethoven["files_not_in_plex"] == 1
    assert beethoven["missing_files"] == [
        "YT-DLP Imports/Pianist/Beethoven Sonatas/02.opus"
    ]


def test_unindexed_counts_are_global_even_when_search_filters_the_visible_rows():
    report = reconcile_inventory(inventory(), [], status="unindexed", query="Beethoven")
    assert report["total_matches"] == 1
    assert report["not_in_plex_audio_files"] == 3
    assert report["not_in_plex_album_folders"] == 2
