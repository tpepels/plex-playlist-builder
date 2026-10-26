"""Read-only, conservative detection of duplicate Plex music library entries.

Matching metadata and duration are evidence of a *possible* duplicate, not
proof that two files have the same audio. Never remove or update Plex items.
"""
from __future__ import annotations

from collections import defaultdict
import csv
import io

from .insights import normalize, timestamp
from .youtube_album import base_album_title

DURATION_TOLERANCE_MS = 2000
TSV_COLUMNS = (
    "group", "kind", "artist", "title", "album", "disc", "track",
    "duration_ms", "rating_key", "added_at",
)


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


def _snapshot(track):
    artist = str(getattr(track, "grandparentTitle", "") or "").strip()
    album = str(getattr(track, "parentTitle", "") or "").strip()
    title = str(getattr(track, "title", "") or "").strip()
    key = str(getattr(track, "ratingKey", "") or "").strip()
    duration = _positive_int(getattr(track, "duration", 0))
    if not key or not artist or not album or not title:
        return None
    added = timestamp(getattr(track, "addedAt", None))
    return {
        "artist": artist, "album": album, "title": title,
        "rating_key": key, "duration_ms": duration,
        "disc": _positive_int(getattr(track, "parentIndex", 0)),
        "track": _positive_int(getattr(track, "index", 0)),
        "added_at": added.isoformat() if added else "",
        "_artist": normalize(artist), "_album": normalize(album),
        "_base_album": normalize(base_album_title(album)),
        "_title": normalize(title),
    }


def _near_duration_clusters(entries):
    """Each cluster has a maximum/minimum duration spread <=2 seconds."""
    sorted_entries = sorted(entries, key=lambda e: (e["duration_ms"], e["rating_key"]))
    clusters = []
    for item in sorted_entries:
        if not clusters or item["duration_ms"] - clusters[-1][0]["duration_ms"] > DURATION_TOLERANCE_MS:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _public(item):
    return {name: value for name, value in item.items() if not name.startswith("_")}


def _as_group(kind, entries):
    ordered = sorted(entries, key=lambda e: (e["_album"], e["disc"], e["track"], e["duration_ms"], e["rating_key"]))
    return {
        "kind": kind,
        "artist": ordered[0]["artist"], "title": ordered[0]["title"],
        "albums": list(dict.fromkeys(item["album"] for item in ordered)),
        "duration_spread_ms": max(x["duration_ms"] for x in ordered) - min(x["duration_ms"] for x in ordered),
        "entries": [_public(item) for item in ordered],
    }


def find_duplicates(tracks):
    """Identify metadata candidates, separately for exact albums and editions.

    'likely' requires artist + album + title + disc/track position, plus
    durations within 2 seconds. 'edition' requires the same base album title
    across different edition labels and a similar duration. Neither category
    proves identical audio; different albums/live performances are excluded.
    """
    items = {}
    for raw in tracks:
        item = _snapshot(raw)
        if item is not None:
            items.setdefault(item["rating_key"], item)

    exact = defaultdict(list)
    editions = defaultdict(list)
    without_duration = 0
    for item in items.values():
        if not item["duration_ms"]:
            without_duration += 1
            continue
        exact[(item["_artist"], item["_album"], item["_title"],
               item["disc"], item["track"])].append(item)
        editions[(item["_artist"], item["_base_album"], item["_title"])].append(item)

    groups = []
    for key in sorted(exact):
        for cluster in _near_duration_clusters(exact[key]):
            if len(cluster) > 1:
                groups.append(_as_group("likely", cluster))

    for key in sorted(editions):
        for cluster in _near_duration_clusters(editions[key]):
            variants = {x["_album"] for x in cluster}
            if len(cluster) > 1 and len(variants) > 1:
                groups.append(_as_group("edition", cluster))

    groups.sort(key=lambda g: (
        0 if g["kind"] == "likely" else 1,
        -len(g["entries"]), normalize(g["artist"]), normalize(g["title"]),
        tuple(normalize(x) for x in g["albums"]),
    ))
    likely = [g for g in groups if g["kind"] == "likely"]
    edition = [g for g in groups if g["kind"] == "edition"]
    return {
        "summary": {
            "library_tracks": len(items),
            "likely_groups": len(likely),
            "likely_extra_entries": sum(len(g["entries"]) - 1 for g in likely),
            "edition_groups": len(edition),
            "edition_entries_to_review": sum(len(g["entries"]) for g in edition),
            "missing_duration": without_duration,
            "duration_tolerance_ms": DURATION_TOLERANCE_MS,
        },
        "groups": groups,
    }


def duplicates_tsv(report):
    """Export every candidate, even those hidden by the browser's display cap."""
    out = io.StringIO()
    writer = csv.DictWriter(out, delimiter="\t", fieldnames=TSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for group_number, group in enumerate(report["groups"], start=1):
        for entry in group["entries"]:
            writer.writerow({
                "group": group_number, "kind": group["kind"],
                "artist": entry["artist"], "title": entry["title"],
                "album": entry["album"], "disc": entry["disc"],
                "track": entry["track"], "duration_ms": entry["duration_ms"],
                "rating_key": entry["rating_key"], "added_at": entry["added_at"],
            })
    return out.getvalue()
