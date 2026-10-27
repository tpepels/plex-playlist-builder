"""Plex cover ZIP import, strict matching, safe storage, auto restoration."""
from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from fastapi import HTTPException

from app import main, playlist_covers as covers
from app import plex_client

JPEG_ONE = b"\xff\xd8\xff\xe0" + b"cover-v1" + b"\xff\xd9"
JPEG_TWO = b"\xff\xd8\xff\xe0" + b"cover-v2" + b"\xff\xd9"
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 13 +
       b"\x00\x00\x00\x00IEND\xaeB\x60\x82")


def archive(items):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, blob in items:
            z.writestr(name, blob)
    return output.getvalue()


class FakePlex:
    def __init__(self, *playlists):
        self.audio = list(playlists)
        self.uploads = []
        self._session = SimpleNamespace(post=lambda *a, **kw: None)

    def playlists(self, playlistType):
        assert playlistType == "audio"
        return self.audio

    def query(self, path, *, method, data):
        self.uploads.append((path, data))


def playlist(title, key):
    return SimpleNamespace(title=title, ratingKey=str(key))


def test_upload_zip_applies_only_exactly_matching_audio_playlists(tmp_path):
    plex = FakePlex(playlist("3 AM", 11), playlist("Morning – Clear Head", 12))
    result = covers.import_cover_zip(archive([
        ("3 AM.jpg", JPEG_ONE),
        ("Morning – Clear Head.png", PNG),
        ("No Plex Playlist.jpg", JPEG_TWO),
        ("manifest.csv", b"details"),
    ]), plex=plex, folder=tmp_path)
    assert result["new"] == 3
    assert result["updated"] == 0
    assert result["applied"] == 2
    assert result["unmatched"] == 1
    assert result["skipped"] == 1
    assert [(p, data) for p, data in plex.uploads] == [
        ("/library/metadata/11/posters", JPEG_ONE),
        ("/library/metadata/12/posters", PNG),
    ]
    assert (tmp_path / "3 AM.jpg").read_bytes() == JPEG_ONE
    assert (tmp_path / "Morning – Clear Head.png").read_bytes() == PNG
    assert (tmp_path / "No Plex Playlist.jpg").read_bytes() == JPEG_TWO


def test_reimport_is_a_new_version_and_replaces_old_format(tmp_path):
    plex = FakePlex(playlist("3 AM", 11))
    first = covers.import_cover_zip(archive([("3 AM.jpg", JPEG_ONE)]),
                                    plex=plex, folder=tmp_path)
    second = covers.import_cover_zip(archive([("3 AM.jpg", JPEG_TWO)]),
                                     plex=plex, folder=tmp_path)
    third = covers.import_cover_zip(archive([("3 AM.png", PNG)]),
                                    plex=plex, folder=tmp_path)
    assert [r["new"] for r in (first, second, third)] == [1, 0, 0]
    assert [r["updated"] for r in (first, second, third)] == [0, 1, 1]
    assert [r["applied"] for r in (first, second, third)] == [1, 1, 1]
    assert not (tmp_path / "3 AM.jpg").exists()
    assert (tmp_path / "3 AM.png").read_bytes() == PNG
    assert len(list(tmp_path.iterdir())) == 1


def test_duplicate_titles_within_zip_are_not_applied(tmp_path):
    result = covers.import_cover_zip(archive([
        ("3 AM.jpg", JPEG_ONE),
        ("3 AM.png", PNG),
        ("Jazz.jpg", JPEG_TWO),
    ]), plex=FakePlex(playlist("Jazz", 44)), folder=tmp_path)
    assert result["new"] == 1
    assert result["applied"] == 1
    assert result["skipped"] == 2
    assert not (tmp_path / "3 AM.jpg").exists()
    assert not (tmp_path / "3 AM.png").exists()


def test_directory_traversal_and_bad_files_never_leave_cover_dir(tmp_path):
    result = covers.import_cover_zip(archive([
        ("../outside.jpg", JPEG_ONE),
        ("subfolder/Hidden.jpg", JPEG_TWO),
        ("C:\\temp\\Outside.jpg", JPEG_ONE),
        (".DS_Store", b"unused"),
        ("Bad.jpg", b"not a JPEG"),
        ("Good.jpg", JPEG_TWO),
    ]), folder=tmp_path)
    assert result["new"] == 1
    assert result["pending"] == 1
    assert result["skipped"] == 5
    assert (tmp_path / "Good.jpg").exists()
    assert not (tmp_path.parent / "outside.jpg").exists()


def test_zip_limits_reject_overlong_archive_before_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(covers, "MAX_ZIP_BYTES", 50)
    with pytest.raises(ValueError, match="70 MiB"):
        covers.import_cover_zip(archive([("Big.jpg", JPEG_ONE)]), folder=tmp_path)
    assert not list(tmp_path.iterdir())


def test_more_than_400_members_rejected(tmp_path):
    payload = archive([(f"cover{i}.jpg", JPEG_ONE) for i in range(401)])
    with pytest.raises(ValueError, match="400 entries"):
        covers.import_cover_zip(payload, folder=tmp_path)
    assert not list(tmp_path.iterdir())


def test_multiple_identically_named_playlists_are_not_guessed(tmp_path):
    plex = FakePlex(playlist("Jazz", 1), playlist("Jazz", 2))
    report = covers.import_cover_zip(archive([("Jazz.jpg", JPEG_ONE)]),
                                      plex=plex, folder=tmp_path)
    assert report["unmatched"] == 1
    assert report["applied"] == 0
    assert not plex.uploads
    assert (tmp_path / "Jazz.jpg").is_file()


def test_unicode_normalization_is_allowed_but_case_folding_is_not(tmp_path):
    plex = FakePlex(playlist("Café", 30))
    result = covers.import_cover_zip(archive([("Cafe\u0301.jpg", JPEG_ONE),
                                               ("CAFÉ.jpg", JPEG_TWO)]),
                                     plex=plex, folder=tmp_path)
    assert result["applied"] == 1
    assert result["unmatched"] == 1
    assert (tmp_path / "Café.jpg").exists()


def test_plex_offline_keeps_files_for_future_retry(tmp_path):
    data = archive([("Jazz.jpg", JPEG_ONE)])
    report = covers.import_cover_zip(data, plex=None, folder=tmp_path)
    assert report["pending"] == 1
    assert (tmp_path / "Jazz.jpg").is_file()
    plex = FakePlex(playlist("Jazz", 3))
    def broken(playlistType):
        raise OSError("temporarily offline")
    plex.playlists = broken
    report = covers.import_cover_zip(data, plex=plex, folder=tmp_path)
    assert report["updated"] == 1
    assert report["pending"] == 1


def test_restore_after_sync_reapplies_saved_cover_and_preserves_track_status(tmp_path, monkeypatch):
    (tmp_path / "Jazz.jpg").write_bytes(JPEG_ONE)
    monkeypatch.setattr(covers, "covers_directory", lambda: tmp_path)
    monkeypatch.setattr(plex_client, "resolve_playlist", lambda plex, music, spec:
                        ([object()], [SimpleNamespace(status="matched", dict=lambda: {"status":"matched"})]))
    class Plex(FakePlex):
        def createPlaylist(self, title, items):
            return playlist(title, 333)
    plex = Plex()
    result = plex_client.sync_playlist(plex, None, "Jazz", {"strict":True, "items":[{}]})
    assert result["status"] == "synced"
    assert result["cover"]["status"] == "applied"
    assert plex.uploads == [("/library/metadata/333/posters", JPEG_ONE)]


def test_restore_failure_does_not_invalidate_successful_sync(tmp_path):
    (tmp_path / "Jazz.jpg").write_bytes(JPEG_ONE)
    class BrokenPlex(FakePlex):
        def query(self, *args, **kwargs):
            raise OSError("Plex poster upload unavailable")
    result = covers.restore_after_sync(BrokenPlex(), playlist("Jazz", 333), folder=tmp_path)
    assert result["status"] == "failed"
    assert "OSError" in result["message"]


def test_existing_symlink_cannot_be_overwritten(tmp_path):
    target = tmp_path.parent / "external-poster.jpg"
    (tmp_path / "Jazz.jpg").symlink_to(target)
    report = covers.import_cover_zip(archive([("Jazz.jpg", JPEG_ONE)]), folder=tmp_path)
    assert report["new"] == 0
    assert report["skipped"] == 1
    assert not target.exists()


class FakeRequest:
    def __init__(self, body, content_type="application/zip"):
        self.headers = {"content-type":content_type, "content-length":str(len(body))}
        self.body = body

    async def stream(self):
        for idx in range(0, len(self.body), 13):
            yield self.body[idx:idx+13]


def test_raw_zip_api_endpoint_persists_and_applies(tmp_path, monkeypatch):
    plex = FakePlex(playlist("Jazz", 3))
    monkeypatch.setattr(main, "connect", lambda: (plex, object()))
    monkeypatch.setattr(covers, "covers_directory", lambda: tmp_path)
    result = asyncio.run(main.import_playlist_cover_archive(
        FakeRequest(archive([("Jazz.jpg", JPEG_ONE)]))))
    assert result["applied"] == 1
    assert (tmp_path / "Jazz.jpg").exists()


def test_raw_zip_api_rejects_non_zip_content_type_and_oversize(tmp_path):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.import_playlist_cover_archive(FakeRequest(b"abc", "text/plain")))
    assert exc.value.status_code == 415
    big = FakeRequest(b"hello")
    big.headers["content-length"] = str(covers.MAX_ZIP_BYTES + 1)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.import_playlist_cover_archive(big))
    assert exc.value.status_code == 413
