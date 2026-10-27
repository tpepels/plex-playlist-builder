"""Standalone cover upload safety checks; no actual Plex server is contacted."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import upload_playlist_covers as covers

JPG = b"\xff\xd8\xff" + bytes(20)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(24)


def test_exact_titles_and_unmatched_images_are_skipped():
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "3 AM.jpg").write_bytes(JPG)
        (folder / "Morning - Clear Head.png").write_bytes(PNG)
        (folder / "morning - clear head.jpg").write_bytes(JPG)
        matches, warnings = covers.find_matches(folder, [
            covers.Playlist("3 AM", "100"), covers.Playlist("Morning - Clear Head", "101")
        ])
    assert [x[0].rating_key for x in matches] == ["100", "101"]
    assert any("NO PLEX MATCH" in x for x in warnings)


def test_conflicting_files_and_ambiguous_playlist_titles_are_rejected():
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "Jazz.jpg").write_bytes(JPG)
        (folder / "Jazz.png").write_bytes(PNG)
        assert not covers.find_matches(folder, [covers.Playlist("Jazz", "1")])[0]
        (folder / "Jazz.png").unlink()
        matches, warnings = covers.find_matches(folder, [
            covers.Playlist("Jazz", "1"), covers.Playlist("Jazz", "2")
        ])
        assert not matches
        assert any("AMBIGUOUS PLAYLIST" in x for x in warnings)


def test_unicode_canonical_equivalence_without_case_folding():
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "Cafe\u0301.jpg").write_bytes(JPG)
        matches, _ = covers.find_matches(folder, [covers.Playlist("Caf\u00e9", "1")])
        assert len(matches) == 1
        matches, _ = covers.find_matches(folder, [covers.Playlist("CAF\u00c9", "1")])
        assert not matches


def test_rejects_fake_images_and_symlinks():
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "Bad.jpg").write_bytes(b"NOT A JPEG")
        (folder / "Linked.png").symlink_to(folder / "Bad.jpg")
        matches, warnings = covers.find_matches(folder, [
            covers.Playlist("Bad", "1"), covers.Playlist("Linked", "2")
        ])
        assert not matches
        assert sum("INVALID IMAGE" in x for x in warnings) == 2


def test_api_filters_out_video_and_photo_playlists():
    xml = (b'<MediaContainer size="2">'
           b'<Playlist playlistType="audio" title="Jazz" ratingKey="11"/>'
           b'<Playlist playlistType="video" title="Jazz" ratingKey="12"/>'
           b'</MediaContainer>')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, count): return xml
    class Opener:
        def open(self, request, timeout):
            assert request.get_header("X-plex-token") == "secret"
            assert "secret" not in request.full_url
            return Response()
    with mock.patch.object(covers.urllib.request, "build_opener", return_value=Opener()):
        result = covers.get_playlists("http://localhost:32400", "secret")
    assert result == [covers.Playlist("Jazz", "11")]


def test_plex_page_size_may_be_shorter_than_requested():
    pages = [
        b'<MediaContainer totalSize="2"><Playlist playlistType="audio" title="One" ratingKey="1"/></MediaContainer>',
        b'<MediaContainer totalSize="2"><Playlist playlistType="audio" title="Two" ratingKey="2"/></MediaContainer>'
    ]
    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, count): return self.data
    class Opener:
        def open(self, request, timeout): return Response(pages.pop(0))
    with mock.patch.object(covers.urllib.request, "build_opener", return_value=Opener()):
        result = covers.get_playlists("http://localhost:32400", "secret")
    assert len(result) == 2
    assert pages == []


def test_default_preview_never_calls_poster_upload():
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "Jazz.jpg").write_bytes(JPG)
        with (mock.patch.object(covers, "load_token", return_value="secret"),
              mock.patch.object(covers, "get_playlists", return_value=[covers.Playlist("Jazz", "7")]),
              mock.patch.object(covers, "upload") as upload,
              contextlib.redirect_stdout(io.StringIO()) as out):
            assert covers.main(["--covers", str(folder)]) == 0
        assert "Dry run" in out.getvalue()
        upload.assert_not_called()


def test_apply_uploads_just_matched_music_posters():
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "Jazz.jpg").write_bytes(JPG)
        with (mock.patch.object(covers, "load_token", return_value="secret"),
              mock.patch.object(covers, "get_playlists", return_value=[covers.Playlist("Jazz", "7")]),
              mock.patch.object(covers, "upload") as upload,
              contextlib.redirect_stdout(io.StringIO())):
            assert covers.main(["--covers", str(folder), "--apply"]) == 0
        assert upload.call_count == 1
        assert upload.call_args.args[2].rating_key == "7"


def test_actual_post_targets_only_plex_playlist_poster_endpoint():
    requests = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response()
    with TemporaryDirectory() as tmp:
        image = Path(tmp) / "Jazz.jpg"
        image.write_bytes(JPG)
        with mock.patch.object(covers.urllib.request, "build_opener", return_value=Opener()):
            covers.upload("http://localhost:32400", "secret", covers.Playlist("Jazz", "7"), image, "image/jpeg")
    assert requests[0].full_url == "http://localhost:32400/library/metadata/7/posters"
    assert requests[0].get_method() == "POST"
    assert requests[0].data == JPG
