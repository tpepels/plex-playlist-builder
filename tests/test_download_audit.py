"""Bounded, offline tests for downloader -> disk -> Plex reconciliation."""
from types import SimpleNamespace as Obj

from app import download_audit as audit
from app import main


def track(path, *, artist="Pianist", album="Piano Sonatas", title="Allegro"):
    return Obj(
        grandparentTitle=artist, parentTitle=album, title=title, ratingKey=1234,
        media=[Obj(parts=[Obj(file=path)])],
    )


def job(id="sonata", **overrides):
    return {
        "id": id, "status": "succeeded",
        "source_title": "Beethoven: Piano Sonata Nos. 13, 14 & 15",
        "source_album_artist": "Ludwig van Beethoven",
        "metadata_artist": "Ludwig van Beethoven",
        "metadata_album": "Beethoven: Piano Sonatas",
        "final_dir": "/data/music/YT-DLP Imports/Ludwig van Beethoven/Beethoven Sonatas",
        "files": ["/data/music/YT-DLP Imports/Ludwig van Beethoven/Beethoven Sonatas/01 Allegro.opus",
                  "/data/music/YT-DLP Imports/Ludwig van Beethoven/Beethoven Sonatas/02 Largo.opus"],
        "match_audit": {"requested_artist": "Beethoven",
                        "requested_album": "Piano Sonatas"},
        "staged_files": 0, **overrides,
    }


def disk(id="sonata", present=2, missing=0):
    return {"job_id": id, "files_recorded": present + missing,
            "files_present": present, "files_missing": missing,
            "final_dir_exists": True, "staged_audio_count": 0}


def test_classical_album_recognized_by_files_even_under_different_plex_metadata():
    tracks = [
        track("/different/mount/YT-DLP Imports/Ludwig van Beethoven/Beethoven Sonatas/01 Allegro.opus",
              artist="Artur Rubinstein", album="Piano Sonatas: 13, 14 & 15"),
        track("/different/mount/YT-DLP Imports/Ludwig van Beethoven/Beethoven Sonatas/02 Largo.opus",
              artist="Artur Rubinstein", album="Piano Sonatas: 13, 14 & 15"),
    ]
    result = audit.reconcile([job()], tracks, query="beethoven", disk=[disk()])
    item = result["items"][0]
    assert item["outcome"] == "indexed"
    assert item["files_indexed"] == 2
    assert item["files_on_disk"] == 2
    assert item["plex_labels"] == [
        {"artist": "Artur Rubinstein", "album": "Piano Sonatas: 13, 14 & 15"}
    ]


def test_published_files_on_disk_but_absent_from_plex_are_flagged():
    result = audit.reconcile([job()], [], query="beethoven", disk=[disk()])
    item = result["items"][0]
    assert item["outcome"] == "missing_from_plex"
    assert item["files_on_disk"] == 2
    assert len(item["unmatched_files"]) == 2


def test_disk_loss_is_not_confused_with_plex_missing_index():
    result = audit.reconcile([job()], [], disk=[disk(present=0, missing=2)])
    assert result["items"][0]["outcome"] == "missing_on_disk"


def test_partial_album_and_partially_missing_storage():
    one = track("/plex/YT-DLP Imports/Ludwig van Beethoven/Beethoven Sonatas/01 Allegro.opus")
    result = audit.reconcile([job()], [one], disk=[disk(present=2, missing=0)])
    assert result["items"][0]["outcome"] == "partially_indexed"
    lost = audit.reconcile([job()], [one], disk=[disk(present=1, missing=1)])
    assert lost["items"][0]["outcome"] == "partially_missing_on_disk"


def test_metadata_waiting_is_not_called_published():
    waiting = job(status="needs_metadata", final_dir="", files=[], staged_files=6)
    data = audit.reconcile([waiting], [], disk=[{
        "job_id": "sonata", "files_present": 0, "files_missing": 0,
        "staged_audio_count": 6,
    }])
    item = data["items"][0]
    assert item["outcome"] == "metadata_required"
    assert item["staged_audio_on_disk"] == 6


def test_path_matching_does_not_guess_from_titles():
    assert audit._path_key("/other/Artist/Album/Sonata.opus") is None
    result = audit.reconcile([job()], [
        track("/some/other/collection/01 Allegro.opus",
              artist="Ludwig van Beethoven", album="Beethoven: Piano Sonatas"),
    ])
    assert result["items"][0]["outcome"] == "missing_from_plex"


def test_ambiguous_track_file_across_different_paths_is_not_a_match():
    result = audit.reconcile([job()], [
        track("/data/music/OTHER/Beethoven Sonatas/01 Allegro.opus"),
    ])
    assert result["items"][0]["files_indexed"] == 0


def test_scan_is_explicit_and_acknowledgement_is_not_completion(monkeypatch):
    calls = []
    section = Obj(update=lambda: calls.append("requested"))
    monkeypatch.setattr(main, "connect", lambda: (None, section))
    response = main.scan_music_for_downloads()
    assert response["status"] == "scan_requested"
    assert calls == ["requested"]


def test_api_audit_uses_current_plex_index_not_insights_cache(monkeypatch):
    song = track("/plex/YT-DLP Imports/Ludwig van Beethoven/Beethoven Sonatas/01 Allegro.opus")
    section = Obj(searchTracks=lambda: [song])
    monkeypatch.setattr(main, "read_downloader_jobs", lambda: [job()])
    monkeypatch.setattr(main, "read_disk_audit", lambda: [disk()])
    monkeypatch.setattr(main, "connect", lambda: (None, section))
    response = main.api_download_audit(q="Beethoven")
    assert response["jobs_considered"] == 1
    assert response["items"][0]["files_indexed"] == 1
    assert response["items"][0]["outcome"] == "partially_indexed"
