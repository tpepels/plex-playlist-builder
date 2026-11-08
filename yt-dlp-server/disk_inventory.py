"""Bounded, disk-first audio inventory; does not depend on download job history.

Every result is based on audio filenames present under the configured music
volume. Directory and file symlinks are not followed. A scan that encounters a
limit or OS error explicitly marks the result incomplete.
"""
from __future__ import annotations

from collections import defaultdict
import os
from pathlib import Path

AUDIO_EXTENSIONS = frozenset({
    ".aac", ".flac", ".m4a", ".mka", ".mp3", ".mp4", ".ogg",
    ".opus", ".wav", ".webm",
})
MAX_AUDIO_FILES = 250_000
MAX_DIRECTORIES = 100_000


def _walk(root, *, file_limit=MAX_AUDIO_FILES, directory_limit=MAX_DIRECTORIES):
    root = Path(root)
    if not root.is_dir():
        return [], [f"Music directory is missing: {root}"], False
    records = []
    errors = []
    complete = True
    directories = 0

    def on_error(error):
        nonlocal complete
        complete = False
        errors.append(str(error)[:240])

    for folder, subdirs, files in os.walk(root, topdown=True, followlinks=False,
                                          onerror=on_error):
        directories += 1
        if directories > directory_limit:
            complete = False
            errors.append(f"Directory safety limit reached ({directory_limit}).")
            break
        # Avoid temporary publish areas, hidden metadata, and symlink targets.
        subdirs[:] = sorted(
            name for name in subdirs
            if not name.startswith(".")
            and not (Path(folder) / name).is_symlink()
        )
        if Path(folder).is_symlink():
            continue
        for filename in sorted(files):
            if filename.startswith(".") or Path(filename).suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            path = Path(folder) / filename
            if path.is_symlink():
                continue
            try:
                info = path.stat()
            except OSError as exc:
                on_error(exc)
                continue
            if not path.is_file():
                continue
            records.append((path.relative_to(root).as_posix(), int(info.st_size),
                            float(info.st_mtime)))
            if len(records) >= file_limit:
                complete = False
                errors.append(f"Audio file safety limit reached ({file_limit}).")
                break
        if not complete and len(records) >= file_limit:
            break
    return records, errors[:20], complete


def inventory_music(music_root, import_subdir="YT-DLP Imports", *,
                    file_limit=MAX_AUDIO_FILES, directory_limit=MAX_DIRECTORIES):
    """Scan the entire music root (not just imports), grouped by disk album folder."""
    root = Path(music_root)
    records, errors, complete = _walk(
        root, file_limit=file_limit, directory_limit=directory_limit)
    imported_parts = tuple(Path(import_subdir).parts)
    groups = defaultdict(list)
    for rel, size, modified in records:
        parts = Path(rel).parts
        imported = (len(parts) >= len(imported_parts) + 3 and
                    parts[:len(imported_parts)] == imported_parts)
        if imported:
            # Keep multidisc subdirectories in the same album.
            album_parts = parts[:len(imported_parts) + 2]
            artist, album = parts[len(imported_parts):len(imported_parts) + 2]
        else:
            album_parts = parts[:-1] or (".",)
            album = album_parts[-1] if album_parts[-1] != "." else "(music root)"
            artist = album_parts[-2] if len(album_parts) >= 2 else ""
        group = "/".join(album_parts)
        groups[group].append({"relative_path": rel, "size_bytes": size,
                              "modified_at": modified})
    albums = []
    for folder, tracks in sorted(groups.items()):
        size = sum(item["size_bytes"] for item in tracks)
        albums.append({
            "folder": folder,
            "artist_folder": tracks and (
                folder.split("/")[-2] if len(folder.split("/")) >= 2 else "") or "",
            "album_folder": folder.split("/")[-1],
            "imported": folder.startswith(import_subdir.rstrip("/") + "/"),
            "track_count": len(tracks), "size_bytes": size,
            "newest_modified_at": max(item["modified_at"] for item in tracks),
            "files": [item["relative_path"] for item in tracks],
        })
    return {
        "root": str(root), "complete": complete, "errors": errors,
        "total_audio_files": len(records), "album_folders": len(albums),
        "albums": albums,
    }


def inventory_staging(temp_root, *, file_limit=MAX_AUDIO_FILES,
                      directory_limit=MAX_DIRECTORIES):
    root = Path(temp_root) / "staging"
    if not root.is_dir():
        return {"root": str(root), "complete": True, "errors": [],
                "total_audio_files": 0, "stages": []}
    records, errors, complete = _walk(
        root, file_limit=file_limit, directory_limit=directory_limit)
    stages = defaultdict(list)
    for rel, size, modified in records:
        stage_id = rel.split("/", 1)[0]
        stages[stage_id].append(rel)
    return {
        "root": str(root), "complete": complete, "errors": errors,
        "total_audio_files": len(records),
        "stages": [{"owner_id": key, "file_count": len(value), "files": value}
                   for key, value in sorted(stages.items())],
    }
