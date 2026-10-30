"""Regression tests for the compact artist/album export."""
from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.album_export import export_album_names
from app import main


def track(artist="", album="", title=""):
    return SimpleNamespace(grandparentTitle=artist, parentTitle=album, title=title)


def parse(tsv):
    return list(csv.DictReader(io.StringIO(tsv), delimiter="\t"))


def test_album_export_one_row_per_artist_album_and_keeps_distinct_editions():
    items = [
        track("Stan Getz", "Getz at the Gate", "First"),
        track("Stan Getz", "Getz at the Gate", "Second"),
        track("Stan Getz", "Getz at the Gate (Live)", "Third"),
        track("Other Artist", "Getz at the Gate", "Another"),
        track("STAN GETZ", "GETZ AT THE GATE", "Duplicate casing"),
        track("Bill Evans", "Waltz for Debby", "Song"),
    ]
    text, count = export_album_names(items)
    assert count == 4
    assert text.startswith("artist\talbum\n")
    assert parse(text) == [
        {"artist": "Bill Evans", "album": "Waltz for Debby"},
        {"artist": "Other Artist", "album": "Getz at the Gate"},
        {"artist": "Stan Getz", "album": "Getz at the Gate"},
        {"artist": "Stan Getz", "album": "Getz at the Gate (Live)"},
    ]
    assert all(set(row) == {"artist", "album"} for row in parse(text))


def test_album_export_handles_unicode_and_missing_metadata():
    items = [
        track("鈴木勲", "Blow Up", "Like It Is"),
        track("Björk", "Vespertine", "Hidden Place"),
        track("Artist", "", "A single"),
        track(None, None, "Unknown"),
    ]
    text, count = export_album_names(items)
    assert count == 2
    assert {"artist": "鈴木勲", "album": "Blow Up"} in parse(text)
    assert {"artist": "Björk", "album": "Vespertine"} in parse(text)


def test_album_export_empty_library_has_header():
    assert export_album_names([]) == ("artist\talbum\n", 0)


def test_album_export_endpoint_returns_download_and_count(monkeypatch):
    music = SimpleNamespace(searchTracks=lambda: [
        track("Bill Evans", "Waltz for Debby", "First"),
        track("Bill Evans", "Waltz for Debby", "Second"),
        track("Chet Baker", "Chet Baker Sings", "Third"),
    ])
    monkeypatch.setattr(main, "connect", lambda: (object(), music))

    response = main.export_album_library()

    assert response.status_code == 200
    assert response.media_type.startswith("text/tab-separated-values")
    assert response.headers["x-plex-album-count"] == "2"
    assert "plex-albums.tsv" in response.headers["content-disposition"]
    assert len(parse(response.body.decode("utf-8"))) == 2


def test_album_export_endpoint_reports_plex_errors(monkeypatch):
    def fail():
        raise ConnectionError("Plex is unavailable")
    monkeypatch.setattr(main, "connect", fail)

    with pytest.raises(HTTPException) as exc:
        main.export_album_library()
    assert exc.value.status_code == 500
