"""Disk-to-Plex reconciliation independent of yt-dlp download job history.

Uses full relative filesystem paths rather than album/composer title guesses.
Supports different mount roots across containers and marks ambiguous file
identities explicitly.
"""
from __future__ import annotations

from collections import defaultdict, Counter
from pathlib import PurePosixPath


def _parts(value):
    if not isinstance(value, str):
        return ()
    return tuple(part for part in PurePosixPath(value.replace("\\", "/")).parts
                 if part not in ("", "/", "."))


def plex_media_index(tracks):
    """Index Plex's actual media locations using last 1-3 path components."""
    by_tail = defaultdict(list)
    known_paths = set()
    for track in tracks:
        for media in getattr(track, "media", None) or []:
            for part in getattr(media, "parts", None) or []:
                full = str(getattr(part, "file", "") or "")
                components = _parts(full)
                if not components or full in known_paths:
                    continue
                known_paths.add(full)
                item = {
                    "components": components, "path": full,
                    "artist": str(getattr(track, "grandparentTitle", "") or ""),
                    "album": str(getattr(track, "parentTitle", "") or ""),
                    "title": str(getattr(track, "title", "") or ""),
                    "rating_key": str(getattr(track, "ratingKey", "") or ""),
                }
                for length in range(1, min(3, len(components)) + 1):
                    by_tail[components[-length:]].append(item)
    return by_tail, len(known_paths)


def lookup_file(relative_path, by_tail):
    """Use an exact suffix of the complete disk-relative path, never a title."""
    components = _parts(relative_path)
    if not components:
        return "unverifiable", []
    key = components[-min(3, len(components)):]
    candidates = [
        item for item in by_tail.get(key, [])
        if len(item["components"]) >= len(components)
        and item["components"][-len(components):] == components
    ]
    if len(candidates) > 1:
        return "ambiguous", candidates
    if len(candidates) == 1:
        return "indexed", candidates
    return "missing", []


def reconcile_inventory(inventory, tracks, *, query="", status="all",
                        offset=0, limit=100):
    if status not in {"all", "missing", "partial", "indexed", "ambiguous"}:
        raise ValueError("Unsupported disk audit status filter.")
    by_tail, plex_files = plex_media_index(tracks)
    albums = inventory.get("albums", [])
    all_rows = []
    counts = Counter()
    q = str(query or "").strip().casefold()
    for album in albums:
        disk_files = [f for f in album.get("files", []) if isinstance(f, str)]
        files_indexed, files_ambiguous = 0, 0
        labels = []
        unmatched = []
        ambiguous = []
        for file in disk_files:
            result, matches = lookup_file(file, by_tail)
            if result == "indexed":
                files_indexed += 1
                match = matches[0]
                label = {"artist": match["artist"], "album": match["album"],
                         "rating_key": match["rating_key"]}
                if label not in labels:
                    labels.append(label)
            elif result == "ambiguous":
                files_ambiguous += 1
                ambiguous.append(file)
            else:
                unmatched.append(file)
        total = len(disk_files)
        if files_ambiguous:
            outcome = "ambiguous"
        elif total > 0 and files_indexed == total:
            outcome = "indexed"
        elif files_indexed:
            outcome = "partial"
        else:
            outcome = "missing"
        counts[outcome] += 1
        row = {
            "folder": album["folder"],
            "artist_folder": album.get("artist_folder", ""),
            "album_folder": album.get("album_folder", ""),
            "imported": bool(album.get("imported")),
            "files_on_disk": total,
            "files_indexed": files_indexed,
            "files_ambiguous": files_ambiguous,
            "status": outcome,
            "plex_labels": labels,
            "missing_files": unmatched[:12],
            "ambiguous_files": ambiguous[:12],
            "size_bytes": album.get("size_bytes", 0),
            "newest_modified_at": album.get("newest_modified_at"),
        }
        if q and not any(q in val.casefold() for val in (
            row["folder"],
            " ".join(disk_files),
            " ".join(item["artist"] + " " + item["album"] for item in labels),
        )):
            continue
        if status != "all" and row["status"] != status:
            continue
        all_rows.append(row)
    # Missing imports and missing albums sort first, while still preserving
    # lexical stability. Pagination is done AFTER scanning every disk album.
    severity = {"missing": 0, "partial": 1, "ambiguous": 2, "indexed": 3}
    all_rows.sort(key=lambda row: (severity[row["status"]],
                                   row["folder"].casefold()))
    staging = inventory.get("staging", {})
    return {
        "disk_scan_complete": bool(inventory.get("complete")) and bool(staging.get("complete")),
        "scan_errors": list(inventory.get("errors") or []) +
                       list(staging.get("errors") or []),
        "music_root": inventory.get("root"),
        "disk_audio_files": inventory.get("total_audio_files", 0),
        "disk_album_folders": inventory.get("album_folders", 0),
        "plex_total_media_files": plex_files,
        "disk_status_counts": dict(counts),
        "total_matches": len(all_rows),
        "offset": offset, "limit": limit,
        "items": all_rows[offset:offset+limit],
        "staging": {
            "audio_files": staging.get("total_audio_files", 0),
            "jobs": staging.get("stages", [])[:100],
        },
        "caveats": (
            "Disk folders, not metadata or old job history, determine the album units. "
            "Files are compared to Plex's actual media paths using complete relative "
            "paths across possibly different container mounts. An absent Plex entry "
            "does not, by itself, identify the cause (scan, mount, permissions, "
            "unsupported media, or ignored files)."),
    }
