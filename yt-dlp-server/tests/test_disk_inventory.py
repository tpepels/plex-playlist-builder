"""Regression coverage for complete disk-first inventories without jobs."""
from disk_inventory import inventory_music, inventory_staging


def make(directory, relative):
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    return path


def test_scans_every_album_folder_even_if_no_download_job_exists(tmp_path):
    root = tmp_path / "music"
    make(root, "YT-DLP Imports/Artur Rubinstein/Beethoven Sonatas/01.opus")
    make(root, "YT-DLP Imports/Artur Rubinstein/Beethoven Sonatas/Disc 2/02.flac")
    make(root, "Other Artist/Other Album/Track.mp3")
    make(root, "YT-DLP Imports/.incoming/unfinished/03.opus")
    (root / "YT-DLP Imports/Artur Rubinstein/Beethoven Sonatas/cover.jpg").write_bytes(b"jpg")

    result = inventory_music(root)
    assert result["complete"] is True
    assert result["album_folders"] == 2
    assert result["total_audio_files"] == 3
    names = {group["folder"]: group for group in result["albums"]}
    beethoven = names["YT-DLP Imports/Artur Rubinstein/Beethoven Sonatas"]
    assert beethoven["track_count"] == 2
    assert beethoven["imported"]
    assert len(beethoven["files"]) == 2
    assert names["Other Artist/Other Album"]["track_count"] == 1


def test_symlinks_outside_root_are_not_followed(tmp_path):
    root = tmp_path / "music"
    remote = tmp_path / "outside"
    make(remote, "Artist/Album/not-ours.opus")
    root.mkdir()
    (root / "shortcut").symlink_to(remote, target_is_directory=True)
    result = inventory_music(root)
    assert result["total_audio_files"] == 0
    assert result["album_folders"] == 0


def test_incomplete_scan_does_not_claim_full_coverage(tmp_path):
    root = tmp_path / "music"
    make(root, "A/A/1.opus")
    make(root, "A/A/2.opus")
    result = inventory_music(root, file_limit=1)
    assert result["complete"] is False
    assert result["errors"]
    assert result["total_audio_files"] == 1


def test_staging_is_scanned_independently_of_download_history(tmp_path):
    make(tmp_path, "staging/orphan-403/Part 1.opus")
    make(tmp_path, "staging/album2/Part 2.opus")
    result = inventory_staging(tmp_path)
    assert result["total_audio_files"] == 2
    assert {row["owner_id"] for row in result["stages"]} == {"orphan-403", "album2"}
    assert result["complete"] is True
