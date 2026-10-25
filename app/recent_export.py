"""Export recently added Plex music as an analysis-ready, track-level TSV.

The time window uses track.addedAt (including partial additions to old albums).
The album limit uses the most recently added albums and includes every track.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Literal


FIELDNAMES = [
    "artist", "album", "title", "disc", "track", "year",
    "duration_ms", "rating_key", "added_at",
]


def _utc(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _row(track, added_at):
    return {
        "artist": getattr(track, "grandparentTitle", "") or "",
        "album": getattr(track, "parentTitle", "") or "",
        "title": getattr(track, "title", "") or "",
        "disc": getattr(track, "parentIndex", "") or "",
        "track": getattr(track, "index", "") or "",
        "year": getattr(track, "year", "") or "",
        "duration_ms": getattr(track, "duration", "") or "",
        "rating_key": str(getattr(track, "ratingKey", "") or ""),
        "added_at": added_at.isoformat(timespec="seconds") if added_at else "",
    }


def _album_key(track):
    key = getattr(track, "parentRatingKey", None)
    return str(key) if key else (
        str(getattr(track, "grandparentTitle", "") or "").casefold(),
        str(getattr(track, "parentTitle", "") or "").casefold(),
    )


def _track_position(track):
    def numeric(value):
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0
    return numeric(getattr(track, "parentIndex", None)), numeric(getattr(track, "index", None))


def export_recent_tracks(
    music,
    mode: Literal["hours", "albums"] = "hours",
    limit: int = 48,
    now: datetime | None = None,
) -> tuple[str, int, int]:
    """Return TSV text, number of tracks, and number of albums.

    - hours: tracks added within the last `limit` hours (not entire old albums).
    - albums: all tracks from the `limit` most recently added albums.

    Sorts albums newest-first, but maintains disc/track order inside an album.
    No matches still produces a TSV with the standard header.
    """
    if mode not in ("hours", "albums"):
        raise ValueError("Mode must be 'hours' or 'albums'.")
    if not isinstance(limit, int) or not 1 <= limit <= (8760 if mode == "hours" else 200):
        raise ValueError("Limit is outside the supported range.")

    now = _utc(now) or datetime.now(timezone.utc)
    grouped = {}
    if mode == "hours":
        cutoff = now - timedelta(hours=limit)
        # Use all tracks so an import containing >50 tracks is not truncated.
        for track in music.searchTracks(sort="addedAt:desc"):
            added = _utc(getattr(track, "addedAt", None))
            if added is None or not cutoff <= added <= now:
                continue
            key = _album_key(track)
            if key not in grouped:
                grouped[key] = {"date": added, "tracks": []}
            if added > grouped[key]["date"]:
                grouped[key]["date"] = added
            grouped[key]["tracks"].append((track, added))
    else:
        # PlexAPI returns the newest N album objects directly.
        albums = music.recentlyAddedAlbums(maxresults=limit)
        for album in albums:
            key = str(getattr(album, "ratingKey", "") or "") or (
                str(getattr(album, "parentTitle", "") or "").casefold(),
                str(getattr(album, "title", "") or "").casefold(),
            )
            if key in grouped:
                continue
            added = _utc(getattr(album, "addedAt", None))
            grouped[key] = {
                "date": added or datetime.min.replace(tzinfo=timezone.utc),
                "tracks": [(t, added) for t in album.tracks()],
            }

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDNAMES, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    seen = set()
    album_count = 0
    for group in sorted(grouped.values(), key=lambda g: g["date"], reverse=True):
        wrote_group = False
        for track, added in sorted(group["tracks"], key=lambda item: _track_position(item[0])):
            key = str(getattr(track, "ratingKey", "") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            writer.writerow(_row(track, added))
            wrote_group = True
        if wrote_group:
            album_count += 1
    return out.getvalue(), sum(1 for _ in out.getvalue().splitlines()[1:]), album_count
