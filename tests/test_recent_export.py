from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.recent_export import export_recent_tracks


NOW = datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)


def track(key, artist, album, title, added, disc=1, index=1, parent_key=None):
    return SimpleNamespace(
        ratingKey=key, parentRatingKey=parent_key or f"album-{artist}-{album}",
        grandparentTitle=artist, parentTitle=album, title=title,
        addedAt=added, parentIndex=disc, index=index, year=2026,
        duration=180000,
    )


class Album:
    def __init__(self, key, artist, name, added_at, tracks):
        self.ratingKey = key
        self.parentTitle = artist
        self.title = name
        self.addedAt = added_at
        self._tracks = tracks

    def tracks(self):
        return self._tracks


class Music:
    def __init__(self, tracks=(), albums=()):
        self.all_tracks = list(tracks)
        self.all_albums = list(albums)
        self.search_sort = None
        self.requested_album_count = None

    def searchTracks(self, *, sort):
        self.search_sort = sort
        return self.all_tracks

    def recentlyAddedAlbums(self, *, maxresults):
        self.requested_album_count = maxresults
        return self.all_albums[:maxresults]


def parse(text):
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def test_last_48_hours_filters_tracks_not_old_albums():
    good = track(1, "New", "Album", "New track", NOW - timedelta(hours=47))
    old = track(2, "New", "Album", "Old track", NOW - timedelta(days=20), index=2)
    second = track(3, "Else", "Second", "Another new", NOW - timedelta(hours=2))
    missing_date = track(4, "Else", "Second", "Date unknown", None)
    music = Music([old, good, missing_date, second])
    text, tracks, albums = export_recent_tracks(music, "hours", 48, now=NOW)
    rows = parse(text)
    assert [int(x["rating_key"]) for x in rows] == [3, 1]
    assert rows[0]["added_at"].startswith("2026-10-25")
    assert tracks == 2
    assert albums == 2
    assert music.search_sort == "addedAt:desc"


def test_last_n_albums_exports_every_track_in_album_and_preserves_album_order():
    newest = Album(10, "A", "Latest", NOW, [
        track(2, "A", "Latest", "Second", NOW - timedelta(days=2), index=2),
        track(1, "A", "Latest", "First", NOW - timedelta(days=2), index=1),
    ])
    older = Album(11, "B", "Previous", NOW - timedelta(days=1), [
        track(3, "B", "Previous", "First", NOW - timedelta(days=5))
    ])
    far_old = Album(12, "C", "Too old", NOW - timedelta(days=8), [
        track(4, "C", "Too old", "Not included", NOW)
    ])
    music = Music(albums=[older, newest, far_old])
    text, tracks, albums = export_recent_tracks(music, "albums", 2, now=NOW)
    rows = parse(text)
    assert [int(x["rating_key"]) for x in rows] == [1, 2, 3]
    assert [r["album"] for r in rows] == ["Latest", "Latest", "Previous"]
    assert music.requested_album_count == 2
    assert tracks == 3
    assert albums == 2
    # The album's addedAt is the criterion in album mode, not the dates of its tracks.
    assert rows[0]["added_at"] == NOW.isoformat(timespec="seconds")


def test_empty_export_has_header_and_zero_counts():
    music = Music([track(1, "Old", "Old", "Old", NOW - timedelta(days=9))])
    text, tracks, albums = export_recent_tracks(music, "hours", 24, now=NOW)
    assert text.startswith("artist\talbum\ttitle\t")
    assert parse(text) == []
    assert tracks == albums == 0


def test_naive_added_at_and_duplicate_track_ids():
    # PlexAPI typically uses aware datetimes; preserve compatibility with a naive UTC datetime.
    t = track(11, "Artist", "Album", "Track", NOW.replace(tzinfo=None))
    music = Music([t, t])
    text, tracks, albums = export_recent_tracks(music, "hours", 1, now=NOW)
    assert tracks == albums == 1
    assert len(parse(text)) == 1


@pytest.mark.parametrize("mode,limit", [("hours", 0), ("albums", 201), ("none", 20)])
def test_invalid_parameters_do_not_export(mode, limit):
    with pytest.raises(ValueError):
        export_recent_tracks(Music(), mode, limit, now=NOW)


def test_http_endpoint_exposes_filtered_tsv_and_download_headers(monkeypatch):
    music = Music(albums=[Album(
        20, "Music", "Album", NOW, [track(25, "Music", "Album", "Track", NOW)]
    )])
    monkeypatch.setattr(main, "connect", lambda: (object(), music))
    response = main.export_recent_library(mode="albums", limit=1)
    assert response.media_type.startswith("text/tab-separated-values")
    assert response.headers["x-plex-track-count"] == "1"
    assert response.headers["x-plex-album-count"] == "1"
    assert "plex-recent-1-albums.tsv" in response.headers["content-disposition"]
    assert parse(response.body.decode("utf-8"))[0]["rating_key"] == "25"


def test_http_endpoint_rejects_too_many_albums_before_connecting():
    with pytest.raises(HTTPException) as error:
        main.export_recent_library(mode="albums", limit=201)
    assert error.value.status_code == 422
