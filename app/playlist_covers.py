"""ZIP-based persistent Plex audio playlist covers.

The file stem is the exact playlist title (Unicode NFC equivalence).
Cover files live on the existing /data volume. Re-importing a filename,
including changing JPEG to PNG, replaces the prior version.
"""
from __future__ import annotations

from collections import defaultdict
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
import threading
import unicodedata
import zipfile

MAX_ZIP_BYTES = 70 * 1024 * 1024
MAX_FILES = 400
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_UNCOMPRESSED = 150 * 1024 * 1024
FORMATS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
_LOCK = threading.RLock()


def covers_directory() -> Path:
    return Path(os.environ.get("PLAYLIST_COVERS_DIR", "/data/playlist-covers"))


def canonical(title: str) -> str:
    return unicodedata.normalize("NFC", title)


def _image_format(data: bytes, suffix: str) -> str:
    mime = FORMATS.get(suffix.lower(), "")
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image is empty or exceeds 12 MiB")
    if mime == "image/png":
        if not (data.startswith(b"\x89PNG\r\n\x1a\n") and
                len(data) >= 33 and data[12:16] == b"IHDR" and
                data[-12:-8] == b"\x00\x00\x00\x00" and data[-8:-4] == b"IEND"):
            raise ValueError("PNG signature or structure is invalid")
    elif mime == "image/jpeg":
        if not (data.startswith(b"\xff\xd8\xff") and data.rstrip().endswith(b"\xff\xd9")):
            raise ValueError("JPEG signature or ending is invalid")
    else:
        raise ValueError("only JPEG and PNG images are allowed")
    return ".png" if mime == "image/png" else ".jpg"


def _valid_name(filename: str) -> bool:
    if not filename or filename != filename.strip() or "/" in filename or "\\" in filename:
        return False
    if filename.startswith(".") or any(ord(char) < 32 or ord(char) == 127 for char in filename):
        return False
    if len(filename.encode("utf-8")) > 250:
        return False
    path = Path(filename)
    return path.stem not in ("", ".", "..") and path.suffix.lower() in FORMATS


def _stored_files(folder: Path, title: str) -> list[Path]:
    """Return matching prior versions, protecting against paths and symlinks."""
    key = canonical(title)
    if not folder.exists():
        return []
    if folder.is_symlink():
        raise ValueError("Cover directory must not be a symbolic link")
    result = []
    for item in folder.iterdir():
        if item.suffix.lower() in FORMATS and canonical(item.stem) == key:
            if item.is_symlink() or not item.is_file():
                raise ValueError("Existing cover is not a regular file: " + item.name)
            result.append(item)
    return result


def stored_cover(title: str, folder: Path | None = None) -> Path | None:
    folder = folder or covers_directory()
    with _LOCK:
        candidates = _stored_files(folder, title)
        if len(candidates) != 1:
            # Ambiguous legacy files are never chosen arbitrarily.
            return None
        return candidates[0]


def _save_cover(folder: Path, title: str, extension: str, data: bytes) -> bool:
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ValueError("Cover directory must not be a symbolic link")
    existing = _stored_files(folder, title)
    filename = canonical(title) + extension
    destination = folder / filename
    if len(filename.encode("utf-8")) > 255:
        raise ValueError("Cover filename is too long for the filesystem")
    if destination.exists() and not destination.is_file():
        raise ValueError("Destination is not a regular file")
    updated = bool(existing)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".cover-", dir=folder, delete=False) as file:
            temp_path = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        temp_path.replace(destination)
        for old in existing:
            if old != destination:
                old.unlink()
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return updated


def _parse_zip(payload: bytes):
    if not payload or len(payload) > MAX_ZIP_BYTES:
        raise ValueError("ZIP must be between 1 byte and 70 MiB")
    candidates = defaultdict(list)
    skipped = []
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES:
                raise ValueError("ZIP contains more than 400 entries")
            total = sum(member.file_size for member in entries)
            if total > MAX_UNCOMPRESSED:
                raise ValueError("Uncompressed ZIP content exceeds 150 MiB")
            for member in entries:
                name = member.filename
                if member.is_dir() or not _valid_name(name):
                    skipped.append({"file": name, "reason": "Not a top-level JPG/PNG file"})
                    continue
                if member.flag_bits & 0x1:
                    skipped.append({"file": name, "reason": "Encrypted entries are unsupported"})
                    continue
                if member.file_size > MAX_IMAGE_BYTES:
                    skipped.append({"file": name, "reason": "Image exceeds 12 MiB"})
                    continue
                key = canonical(Path(name).stem)
                candidates[key].append(member)
            accepted = []
            for key, members in candidates.items():
                if len(members) > 1:
                    for member in members:
                        skipped.append({"file": member.filename, "reason": "Multiple images for same playlist in ZIP"})
                    continue
                member = members[0]
                try:
                    # Bounded read, even if a malicious ZIP lies about file_size.
                    with archive.open(member) as stream:
                        data = stream.read(MAX_IMAGE_BYTES + 1)
                    ext = _image_format(data, Path(member.filename).suffix)
                    accepted.append((key, ext, data, member.filename))
                except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
                    skipped.append({"file": member.filename, "reason": "Invalid image or ZIP entry"})
        return accepted, skipped
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as exc:
        raise ValueError("Not a valid ZIP archive") from exc


def apply_poster(plex, playlist, image: Path):
    """Use Plex's raw poster upload endpoint; it auto-selects the new poster."""
    content = image.read_bytes()
    _image_format(content, image.suffix)
    key = str(getattr(playlist, "ratingKey", ""))
    if not re.fullmatch(r"\d+", key):
        raise ValueError("Playlist has no valid Plex rating key")
    plex.query(
        f"/library/metadata/{key}/posters",
        method=plex._session.post, data=content,
    )


def restore_after_sync(plex, playlist, folder: Path | None = None) -> dict:
    title = str(getattr(playlist, "title", ""))
    with _LOCK:
        image = None
        try:
            image = stored_cover(title, folder)
            if image is None:
                return {"status": "none"}
            apply_poster(plex, playlist, image)
            return {"status": "applied", "file": image.name}
        except Exception as exc:
            # A playlist was successfully recreated; artwork failure must not
            # lie about that sync outcome or trigger a retry that deletes it.
            return {"status": "failed", "file": image.name if image else "",
                    "message": f"Cover restore failed: {type(exc).__name__}"}


def import_cover_zip(payload: bytes, plex=None, folder: Path | None = None) -> dict:
    """Validate the whole archive before writing any file; then save and apply."""
    accepted, skipped = _parse_zip(payload)
    if not accepted:
        raise ValueError("ZIP contains no valid, unambiguous top-level JPG/PNG images")
    folder = folder or covers_directory()
    stored = []
    with _LOCK:
        for title, ext, data, original in accepted:
            try:
                replaced = _save_cover(folder, title, ext, data)
                stored.append({"title": title, "file": title + ext,
                               "source": original, "version": "updated" if replaced else "new",
                               "status": "stored", "reason": ""})
            except (OSError, ValueError) as exc:
                skipped.append({"file": original, "reason": str(exc)})
        if plex is None:
            for item in stored:
                item["status"] = "pending"
                item["reason"] = "Saved; Plex is unavailable"
        else:
            playlists = defaultdict(list)
            try:
                for playlist in plex.playlists(playlistType="audio"):
                    playlists[canonical(str(playlist.title))].append(playlist)
            except Exception:
                for item in stored:
                    item["status"] = "pending"
                    item["reason"] = "Saved, but Plex playlist listing is unavailable"
            else:
                for item in stored:
                    matches = playlists.get(item["title"], [])
                    if not matches:
                        item["status"] = "unmatched"
                        item["reason"] = "No audio playlist with this exact name"
                    elif len(matches) > 1:
                        item["status"] = "unmatched"
                        item["reason"] = "Multiple audio playlists share this name"
                    else:
                        try:
                            apply_poster(plex, matches[0], folder / item["file"])
                            item["status"] = "applied"
                        except Exception as exc:
                            item["status"] = "failed"
                            item["reason"] = f"Plex cover upload failed: {type(exc).__name__}"
    return {
        "count": len(accepted),
        "new": sum(x["version"] == "new" for x in stored),
        "updated": sum(x["version"] == "updated" for x in stored),
        "applied": sum(x["status"] == "applied" for x in stored),
        "unmatched": sum(x["status"] == "unmatched" for x in stored),
        "pending": sum(x["status"] == "pending" for x in stored),
        "failed": sum(x["status"] == "failed" for x in stored),
        "skipped": len(skipped),
        "files": stored, "skipped_files": skipped,
    }
