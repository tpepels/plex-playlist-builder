"""Read-only bounded Plex validation for AudioMuse provider identifiers.

This module does not fetch the full music library or modify Plex data.
"""
from __future__ import annotations

from plexapi.exceptions import NotFound


def verify_plex_track_keys(plex, raw_keys: list[str]) -> dict:
    if not 1 <= len(raw_keys) <= 25:
        raise ValueError("Supply between 1 and 25 track IDs per request.")
    result = []
    for raw_key in raw_keys:
        key = str(raw_key).strip()
        if not key.isascii() or not key.isdecimal():
            result.append({"id": key, "status": "invalid"})
            continue
        try:
            track = plex.fetchItem(int(key))
        except NotFound:
            result.append({"id": key, "status": "missing"})
            continue
        # A track rating key must refer to a track; never accept albums/artists.
        if getattr(track, "TYPE", getattr(track, "type", "")) != "track":
            result.append({"id": key, "status": "not_track"})
            continue
        result.append({
            "id": key,
            "status": "matched",
            "title": str(getattr(track, "title", "") or ""),
            "artist": str(getattr(track, "grandparentTitle", "") or ""),
            "album": str(getattr(track, "parentTitle", "") or ""),
        })
    return {
        "checked": len(result),
        "matched": sum(item["status"] == "matched" for item in result),
        "results": result,
    }
