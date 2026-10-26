"""Read-only, album-level duplicate review for Plex music.

Each physical album folder is compared as a *whole release*, not track by
track in the UI. A matching tracklist and close durations are only metadata
evidence. No filesystem access, audio fingerprinting, or deletion occurs.
"""
from __future__ import annotations

from collections import defaultdict
import csv
import io
from pathlib import PurePosixPath
import re

from .insights import normalize, timestamp
from .youtube_album import base_album_title


DURATION_TOLERANCE_MS = 2000
DISC_DIR = re.compile(r"(?i)^(?:disc|disk|cd)[\s._-]*0*[1-9][0-9]*$")
TSV_COLUMNS = (
    "group", "match", "artist", "album", "release", "directory",
    "folder_review_candidate", "folder_status", "tracks", "matched_tracks",
    "total_duration_ms", "album_rating_key", "track_rating_keys",
    "media_files", "missing_media_paths",
)


def _number(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return 0


def _album_directory(file_path):
    """A dedicated album folder (possibly above 'Disc 1'), never / or /Music."""
    if not isinstance(file_path, str) or not file_path.startswith("/") or "\x00" in file_path:
        return ""
    path = PurePosixPath(file_path)
    if ".." in file_path.split("/") or len(path.parts) < 4:
        return ""
    directory = path.parent
    if DISC_DIR.fullmatch(directory.name):
        directory = directory.parent
    # /music/artist/album has 3 non-root parts; avoid proposing artist/root dirs.
    if len(directory.parts) < 4:
        return ""
    return str(directory)


def _media_paths(track):
    """Return file paths from every Plex Media/Part variant for this track."""
    paths = set()
    for media in getattr(track, "media", None) or ():
        for part in getattr(media, "parts", None) or ():
            path = getattr(part, "file", None)
            if isinstance(path, str) and path:
                paths.add(path)
    return sorted(paths)


def _snapshot(track):
    artist = str(getattr(track, "grandparentTitle", "") or "").strip()
    album = str(getattr(track, "parentTitle", "") or "").strip()
    title = str(getattr(track, "title", "") or "").strip()
    key = str(getattr(track, "ratingKey", "") or "").strip()
    if not (artist and album and title and key):
        return None
    album_id = str(getattr(track, "parentRatingKey", "") or "").strip()
    added = timestamp(getattr(track, "addedAt", None))
    return {
        "artist": artist, "album": album, "title": title, "rating_key": key,
        "album_rating_key": album_id,
        "duration_ms": _number(getattr(track, "duration", 0)),
        "disc": _number(getattr(track, "parentIndex", 0)),
        "position": _number(getattr(track, "index", 0)),
        "added_at": added.isoformat() if added else "",
        "paths": _media_paths(track),
        "_artist": normalize(artist),
        "_album": normalize(album),
        "_base": normalize(base_album_title(album)),
        "_title": normalize(title),
    }


def _track_id(item):
    return item["disc"], item["position"], item["_title"]


def _compare(left, right):
    """Full requires exactly the same indexed tracklist with durations ±2s."""
    a = {_track_id(t): t for t in left["tracks"]}
    b = {_track_id(t): t for t in right["tracks"]}
    # A repeated disc/position/title, missing position or unknown duration makes
    # an otherwise identical-looking release unsuitable as a folder candidate.
    usable_a = len(a) == len(left["tracks"]) and all(
        t["disc"] and t["position"] and t["duration_ms"] for t in left["tracks"])
    usable_b = len(b) == len(right["tracks"]) and all(
        t["disc"] and t["position"] and t["duration_ms"] for t in right["tracks"])
    common = set(a) & set(b)
    matches = sum(
        abs(a[k]["duration_ms"] - b[k]["duration_ms"]) <= DURATION_TOLERANCE_MS
        and bool(a[k]["duration_ms"]) and bool(b[k]["duration_ms"])
        for k in common
    )
    full = (usable_a and usable_b and len(a) == len(b)
            and matches == len(a) and len(a) > 0)
    return ("full" if full else "partial", matches)


def _public_release(r):
    return {
        "id": r["id"], "album": r["album"], "directory": r["directory"],
        "album_rating_key": r["album_rating_key"],
        "tracks": len(r["tracks"]), "total_duration_ms": sum(t["duration_ms"] for t in r["tracks"]),
        "track_rating_keys": [t["rating_key"] for t in r["tracks"]],
        "media_files": sum(len(t["paths"]) for t in r["tracks"]),
        "missing_media_paths": sum(not t["paths"] for t in r["tracks"]),
        "folder_status": r["folder_status"],
        "folder_review_candidate": r["folder_review_candidate"],
        "match": r["match"],
        "matched_tracks": r["matched_tracks"],
        "track_details": [{
            "title": t["title"], "disc": t["disc"], "track": t["position"],
            "duration_ms": t["duration_ms"], "rating_key": t["rating_key"],
            "files": t["paths"],
        } for t in r["tracks"]],
    }


def find_duplicates(tracks):
    """Compare complete album copies, returning folder paths only for review.

    Plex can merge multiple physical media versions under one rating key, so
    both rating keys AND all Media/Part file paths must be preserved.
    Directories are never declared safe to delete: unindexed files, sidecars,
    quality differences and playlists cannot be verified with metadata alone.
    """
    snapshots = {}
    for raw in tracks:
        item = _snapshot(raw)
        if item:
            if item["rating_key"] in snapshots:
                # The same Plex track can be returned twice with differing
                # populated media info; merge its media paths, not tracks.
                old = snapshots[item["rating_key"]]
                old["paths"] = sorted(set(old["paths"]) | set(item["paths"]))
            else:
                snapshots[item["rating_key"]] = item

    releases = {}
    # Folder usage is counted over the whole library, not just matching titles.
    # An artist folder, shared directory or directory holding other albums may
    # not be presented as a clean album-folder candidate.
    directory_owners = defaultdict(set)
    missing_media = 0
    for item in snapshots.values():
        paths = item["paths"]
        if not paths:
            missing_media += 1
        # A file can have multiple Media parts and one Plex key. Make a
        # per-physical-folder track record without counting it twice.
        folder_paths = defaultdict(list)
        for path in paths:
            directory = _album_directory(path)
            if directory:
                folder_paths[directory].append(path)
                directory_owners[directory].add(
                    (item["_artist"], item["_base"], item["album_rating_key"]))
        if not folder_paths:
            folder_paths[""] = []

        for folder, files in folder_paths.items():
            # The parent album ID distinguishes unrelated Plex album objects;
            # the physical directory separates multiple copies under one ID.
            album_identity = item["album_rating_key"] or (item["_artist"] + ":" + item["_album"])
            identity = (item["_artist"], item["_base"], album_identity, folder)
            if identity not in releases:
                releases[identity] = {
                    "id": len(releases) + 1,
                    "artist": item["artist"], "album": item["album"],
                    "album_rating_key": item["album_rating_key"],
                    "directory": folder, "tracks": [],
                    "folder_status": "No Plex media directory" if not folder else "Review",
                    "folder_review_candidate": False,
                    "match": "none", "matched_tracks": 0,
                }
            copy = dict(item)
            copy["paths"] = sorted(files)
            # Some Plex metadata appears more than once for one physical path.
            # A physical file may still be a multi-part file; keep one track.
            if not any(t["rating_key"] == copy["rating_key"] for t in releases[identity]["tracks"]):
                releases[identity]["tracks"].append(copy)

    for r in releases.values():
        r["tracks"].sort(key=lambda x: (x["disc"], x["position"], x["_title"], x["rating_key"]))
        folder = r["directory"]
        if not folder:
            r["folder_status"] = "Plex did not expose a dedicated album directory"
        elif len(directory_owners[folder]) != 1:
            r["folder_status"] = "Shared directory - not a removal candidate"
        elif not all(t["paths"] for t in r["tracks"]):
            r["folder_status"] = "Some track paths are missing"
        else:
            r["folder_status"] = "Dedicated album folder in Plex - check files manually"

    by_title = defaultdict(list)
    for r in releases.values():
        by_title[(normalize(r["artist"]), normalize(base_album_title(r["album"])))].append(r)

    groups = []
    for (artist_key, album_key), albums in sorted(by_title.items()):
        if len(albums) < 2:
            continue
        match_pairs = []
        full_exact_pairs = []
        full_edition_pairs = []
        for i, left in enumerate(albums):
            for right in albums[i + 1:]:
                # Two indexed releases occupying the same physical directory
                # do not represent two independently removable copies.
                if left["directory"] and left["directory"] == right["directory"]:
                    continue
                relationship, matched = _compare(left, right)
                if matched == 0:
                    continue
                if relationship == "full":
                    if normalize(left["album"]) == normalize(right["album"]):
                        kind = "full"
                        full_exact_pairs.append((left, right))
                    else:
                        kind = "edition"
                        full_edition_pairs.append((left, right))
                else:
                    kind = "partial"
                match_pairs.append((left, right, kind, matched))

        if not match_pairs:
            continue

        shown = []
        for r in albums:
            related = [p for p in match_pairs if p[0] is r or p[1] is r]
            if not related:
                continue
            priorities = {"full": 0, "edition": 1, "partial": 2}
            best = min(related, key=lambda p: priorities[p[2]])
            r["match"] = best[2]
            r["matched_tracks"] = max(p[3] for p in related)
            is_exact_copy = any(r is left or r is right for left, right in full_exact_pairs)
            r["folder_review_candidate"] = (
                is_exact_copy and bool(r["directory"])
                and r["folder_status"] == "Dedicated album folder in Plex - check files manually"
                and all(t["paths"] for t in r["tracks"])
            )
            shown.append(_public_release(r))

        status = ("full" if full_exact_pairs else
                  "edition" if full_edition_pairs else "partial")
        group_name = base_album_title(albums[0]["album"])
        groups.append({
            "artist": albums[0]["artist"], "album": group_name,
            "kind": status, "releases": shown,
            "total_copies": len(shown),
            "reviewable_folders": sum(x["folder_review_candidate"] for x in shown),
        })

    priority = {"full": 0, "edition": 1, "partial": 2}
    groups.sort(key=lambda g: (
        priority[g["kind"]], -g["reviewable_folders"],
        normalize(g["artist"]), normalize(g["album"]),
    ))
    return {
        "summary": {
            "library_tracks": len(snapshots),
            "album_groups": len(groups),
            "full_album_groups": sum(g["kind"] == "full" for g in groups),
            "edition_groups": sum(g["kind"] == "edition" for g in groups),
            "partial_groups": sum(g["kind"] == "partial" for g in groups),
            "folder_review_candidates": sum(g["reviewable_folders"] for g in groups),
            "tracks_without_media_path": missing_media,
            "duration_tolerance_ms": DURATION_TOLERANCE_MS,
        },
        "groups": groups,
    }


def duplicates_tsv(report):
    """One TSV row per *album copy*, listing its folder and track rating keys."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=TSV_COLUMNS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for number, group in enumerate(report["groups"], 1):
        for release in group["releases"]:
            writer.writerow({
                "group": number, "match": release["match"],
                "artist": group["artist"], "album": group["album"],
                "release": release["album"], "directory": release["directory"],
                "folder_review_candidate": release["folder_review_candidate"],
                "folder_status": release["folder_status"],
                "tracks": release["tracks"], "matched_tracks": release["matched_tracks"],
                "total_duration_ms": release["total_duration_ms"],
                "album_rating_key": release["album_rating_key"],
                "track_rating_keys": ";".join(release["track_rating_keys"]),
                "media_files": release["media_files"],
                "missing_media_paths": release["missing_media_paths"],
            })
    return out.getvalue()
