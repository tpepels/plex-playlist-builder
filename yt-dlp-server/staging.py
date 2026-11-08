import ctypes
import errno
import json
import os
import re
import shutil
import subprocess
import uuid
from collections import Counter
from pathlib import Path

from mutagen import File as MutagenFile

MEDIA_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mka", ".mp3", ".mp4",
    ".ogg", ".opus", ".wav", ".webm",
}
UNKNOWN_VALUES = {
    "", "unknown", "unknown album", "[unknown album]",
    "unknown artist", "[unknown artist]", "untitled",
}
SOURCE_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{6,})\](?:\.[^.]+)?$")


def safe_component(value):
    value = str(value or "").strip()
    value = value.replace("/", "／").replace("\\", "＼")
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:160]


def clean_album_name(value):
    value = safe_component(value)
    if value.lower() in UNKNOWN_VALUES:
        return ""
    value = re.sub(r"^album\s*[-–—:]\s*", "", value, flags=re.IGNORECASE).strip()
    if value.lower() in UNKNOWN_VALUES:
        return ""
    return value


def clean_artist_name(value):
    value = safe_component(value)
    value = re.sub(r"\s+-\s+Topic$", "", value, flags=re.IGNORECASE).strip()
    if value.lower() in UNKNOWN_VALUES:
        return ""
    return value


def staging_owner_id(job):
    """Never allow a saved job ID to escape the dedicated staging tree."""
    ident = str(job.get("staging_owner_id") or job.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", ident):
        raise ValueError("Invalid job staging identity; refusing filesystem operation")
    return ident


def stage_dir(temp_dir, job):
    parent = Path(temp_dir) / "staging"
    path = parent / staging_owner_id(job)
    # Reading/publishing an existing symlinked stage would otherwise let a
    # malformed saved job import files from outside the private staging area.
    if parent.is_symlink() or path.is_symlink():
        raise ValueError("Staging path must not be a symbolic link")
    return path


def ensure_stage_dir(temp_dir, job):
    path = stage_dir(temp_dir, job)
    if path.is_symlink():
        raise ValueError("Staging path must not be a symbolic link")
    path.mkdir(parents=True, exist_ok=True)
    (path / ".tmp").mkdir(parents=True, exist_ok=True)
    return path


def staged_ytdlp_output_template():
    return (
        "%(playlist_index,track_number&{:02d} - |)s"
        "%(track,title).180S [%(id)s].%(ext)s"
    )


def staged_spotdl_output_template(temp_dir, job):
    root = ensure_stage_dir(temp_dir, job)
    return str(
        root
        / "{album-artist}"
        / "{album}"
        / "{track-number} - {title} [{track-id}].{output-ext}"
    )


def media_files(root):
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() in MEDIA_EXTENSIONS
        and ".tmp" not in path.parts
    )


def load_info_jsons(root):
    infos = []
    for path in Path(root).rglob("*.info.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data["_sidecar_path"] = str(path)
            infos.append(data)
    return infos


def source_id_from_filename(path):
    match = SOURCE_ID_RE.search(Path(path).name)
    return match.group(1) if match else None


def title_artist(value):
    value = str(value or "").strip()
    for separator in (" - ", " – ", " — "):
        if separator not in value:
            continue
        prefix, _ = value.split(separator, 1)
        prefix = clean_artist_name(prefix)
        if prefix and len(prefix) <= 80:
            return prefix
    return ""


def strict_music_artist(info):
    for key in ("artist", "album_artist"):
        value = clean_artist_name(info.get(key))
        if value:
            return value

    for key in ("uploader", "channel"):
        raw = str(info.get(key) or "").strip()
        if re.search(r"\s+-\s+Topic$", raw, flags=re.IGNORECASE):
            value = clean_artist_name(raw)
            if value:
                return value
    return ""


def trustworthy_track_artist(info):
    return (
        strict_music_artist(info)
        or title_artist(info.get("track") or info.get("title"))
    )


def unique_values(values):
    values = [value for value in values if value]
    return list(dict.fromkeys(values))


def most_common(values):
    values = [value for value in values if value]
    if not values:
        return ""
    return Counter(values).most_common(1)[0][0]


def analyze_ytdlp(stage, job):
    infos = load_info_jsons(stage)
    files = media_files(stage)
    info_by_id = {
        str(info.get("id")): info
        for info in infos
        if info.get("id")
    }

    playlist_titles = [
        clean_album_name(
            info.get("playlist_title")
            or info.get("playlist")
            or info.get("playlist_name")
        )
        for info in infos
    ]
    explicit_albums = [clean_album_name(info.get("album")) for info in infos]

    if job.get("collection_mode", job.get("playlist")):
        album = (
            clean_album_name(job.get("source_title"))
            or most_common(playlist_titles)
            or most_common(explicit_albums)
        )
    else:
        album = most_common(explicit_albums)

    explicit_album_artists = unique_values(
        clean_artist_name(info.get("album_artist")) for info in infos
    )
    strict_track_artists = [
        strict_music_artist(info)
        for info in infos
    ]
    unique_strict_artists = unique_values(strict_track_artists)

    hinted_artist = clean_artist_name(job.get("source_album_artist"))
    if hinted_artist and hinted_artist != "Various Artists":
        album_artist = hinted_artist
    elif len(explicit_album_artists) == 1:
        album_artist = explicit_album_artists[0]
    elif len(unique_strict_artists) == 1:
        album_artist = unique_strict_artists[0]
    elif len(unique_strict_artists) > 1:
        album_artist = "Various Artists"
    else:
        album_artist = ""

    records = []
    for path in files:
        source_id = source_id_from_filename(path)
        info = info_by_id.get(source_id, {})
        artist = trustworthy_track_artist(info)
        if not artist and album_artist and album_artist != "Various Artists":
            artist = album_artist
        records.append({
            "path": str(path),
            "source_id": source_id,
            "artist": artist,
        })

    return {
        "artist": album_artist,
        "album": album,
        "records": records,
        "info_count": len(infos),
        "media_count": len(files),
    }


def analyze_spotdl(stage, job):
    files = media_files(stage)
    artists = []
    albums = []
    records = []

    root = Path(stage)
    for path in files:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        parts = rel.parts
        artist = clean_artist_name(parts[0]) if len(parts) >= 3 else ""
        album = clean_album_name(parts[1]) if len(parts) >= 3 else ""
        if artist:
            artists.append(artist)
        if album:
            albums.append(album)
        records.append({
            "path": str(path),
            "source_id": source_id_from_filename(path),
            "artist": "",
        })

    unique_artists = unique_values(artists)
    unique_albums = unique_values(albums)
    album_artist = unique_artists[0] if len(unique_artists) == 1 else ""
    album = unique_albums[0] if len(unique_albums) == 1 else ""

    return {
        "artist": album_artist,
        "album": album,
        "records": records,
        "info_count": 0,
        "media_count": len(files),
    }


def analyze_stage(temp_dir, job):
    stage = stage_dir(temp_dir, job)
    if job.get("source") == "spotify":
        result = analyze_spotdl(stage, job)
    else:
        result = analyze_ytdlp(stage, job)
    result["stage_dir"] = str(stage)
    result["needs_metadata"] = not bool(result["artist"] and result["album"])
    return result


def _set_mutagen_tag(audio, key, value):
    if not value:
        return False
    try:
        audio[key] = [value]
        return True
    except (KeyError, TypeError, ValueError):
        return False


def _retag_with_mutagen(path, album, album_artist, track_artist=""):
    """Edit tags in place so embedded artwork/auxiliary streams stay intact."""
    try:
        audio = MutagenFile(str(path), easy=True)
    except Exception:
        return False
    if audio is None:
        return False

    try:
        if audio.tags is None:
            audio.add_tags()

        if not _set_mutagen_tag(audio, "album", album):
            return False

        # albumartist is the canonical easy-tag spelling. Vorbis/Opus/FLAC
        # comments accept it directly; EasyID3/EasyMP4 map it appropriately.
        if not _set_mutagen_tag(audio, "albumartist", album_artist):
            return False

        if track_artist and not _set_mutagen_tag(audio, "artist", track_artist):
            return False

        audio.save()
        return True
    except Exception:
        return False


def _retag_audio_only_with_ffmpeg(path, album, album_artist, track_artist=""):
    """Fallback for formats mutagen cannot edit.

    Only map the primary audio stream. Mapping every input stream is unsafe for
    Opus/Ogg because ffmpeg can expose embedded artwork as another stream that
    the output container cannot mux.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.stem}.retag-{uuid.uuid4().hex[:8]}{path.suffix}")
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(path),
        "-map", "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-c:a", "copy",
        "-map_metadata", "0",
        "-metadata", f"album={album}",
        "-metadata", f"album_artist={album_artist}",
        "-metadata", f"albumartist={album_artist}",
    ]
    if track_artist:
        cmd.extend(["-metadata", f"artist={track_artist}"])
    cmd.append(str(tmp))

    try:
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Metadata update timed out for {path.name}; the original "
                "staged file was preserved. Retry after checking disk and audio."
            ) from None
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:800]
            raise RuntimeError(
                f"metadata update failed for {path.name}: "
                f"{detail or completed.returncode}"
            )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def retag_media(path, album, album_artist, track_artist=""):
    path = Path(path)
    if _retag_with_mutagen(path, album, album_artist, track_artist):
        return
    _retag_audio_only_with_ffmpeg(path, album, album_artist, track_artist)


def discard_stage(temp_dir, job):
    """Delete an unpublished collection's staging directory."""
    path = stage_dir(temp_dir, job)
    shutil.rmtree(path, ignore_errors=True)
    return path


def cleanup_interrupted_publications(import_root):
    """Remove only abandoned hidden copies made by this application.

    Called before starting the download worker after a container restart,
    when no publication can be in progress. Source audio still resides in
    the staging directory until the publication succeeds. Never touch
    unknown directories or symlinks in the music library.
    """
    incoming = Path(import_root) / ".incoming"
    if not incoming.is_dir() or incoming.is_symlink():
        return 0
    count = 0
    for entry in incoming.iterdir():
        if (entry.is_dir() and not entry.is_symlink()
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}-[0-9a-f]{8}",
                                 entry.name)):
            shutil.rmtree(entry)
            count += 1
    try:
        if not any(incoming.iterdir()):
            incoming.rmdir()
    except OSError:
        pass
    return count


def _exchange_directories(candidate, existing):
    """Atomically replace a whole album tree, including on nonempty targets.

    Linux RENAME_EXCHANGE retains the previous tree at candidate until cleanup.
    A filesystem not supporting atomic exchange fails without altering the
    published album. Never fall back to per-track moves into a scanned album.
    """
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "Filesystem does not support atomic album replacement")
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p,
        ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(candidate), -100,
                 os.fsencode(existing), 2) != 0:  # RENAME_EXCHANGE
        failure = ctypes.get_errno()
        raise OSError(
            failure,
            "Atomic album exchange failed; existing library album was preserved",
        )


def _publish_album_tree(import_root, incoming_parent, job, artist, album, files):
    """Build an entire album out of sight, then commit the complete tree.

    files is a sequence of (staged Path, output filename) pairs. Existing
    files, including cover art and custom metadata, remain in the snapshot
    unless force explicitly replaces a matching staged audio filename.
    """
    final_dir = Path(import_root) / safe_component(artist) / safe_component(album)
    if final_dir.is_symlink() or final_dir.parent.is_symlink():
        raise RuntimeError("Refusing to publish through a symlinked library album")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    if incoming_parent.is_symlink():
        raise RuntimeError("Refusing symlinked hidden publication directory")
    incoming_parent.mkdir(parents=True, exist_ok=True)
    incoming = incoming_parent / f"{staging_owner_id(job)}-{uuid.uuid4().hex[:8]}"
    incoming.mkdir(parents=True, exist_ok=False)
    paths = []
    try:
        if final_dir.exists():
            if not final_dir.is_dir():
                raise RuntimeError("An existing non-album file blocks publication")
            shutil.copytree(final_dir, incoming, dirs_exist_ok=True, symlinks=True)

        for staged, name in files:
            destination = incoming / name
            if destination.is_symlink():
                raise RuntimeError("Refusing to replace a symlinked library track")
            if not destination.exists() or job.get("force"):
                shutil.copy2(staged, destination)
            paths.append(str(final_dir / name))

        if final_dir.exists():
            _exchange_directories(incoming, final_dir)
        else:
            os.replace(incoming, final_dir)
    except Exception:
        # The staged originals and pre-existing published album were untouched.
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    else:
        # After an exchange, incoming contains the previous published tree.
        # A crash here leaves only an unscanned, recognizable hidden copy;
        # startup cleanup removes it, while the complete album stays visible.
        shutil.rmtree(incoming, ignore_errors=True)
        return str(final_dir), paths


def _cleanup_incoming_parent(incoming_parent):
    try:
        if incoming_parent.is_dir() and not any(incoming_parent.iterdir()):
            incoming_parent.rmdir()
    except OSError:
        pass


def publish_spotdl_stage(temp_dir, import_root, job, *, defer_cleanup=False):
    """Publish each Spotify album atomically, preserving other albums on error."""
    stage = stage_dir(temp_dir, job)
    files = media_files(stage)
    if not files:
        return {
            "status": "error",
            "message": "No staged audio files were found.",
            "artist": "", "album": "", "records": [],
            "info_count": 0, "media_count": 0,
            "stage_dir": str(stage), "needs_metadata": False,
        }

    import_root = Path(import_root)
    import_root.mkdir(parents=True, exist_ok=True)
    incoming_parent = import_root / ".incoming"
    grouped = {}
    for source in files:
        rel = source.relative_to(stage)
        if len(rel.parts) < 3:
            raise RuntimeError(f"Spotify stage lacks Artist/Album folders: {rel}")
        artist = clean_artist_name(rel.parts[0])
        album = clean_album_name(rel.parts[1])
        if not artist or not album:
            raise RuntimeError(f"Spotify album metadata unresolved: {rel}")
        grouped.setdefault((artist, album), []).append((source, source.name))

    final_files = []
    final_dirs = []
    # A multi-album playlist may legitimately publish some complete albums
    # before a later album fails. Replaying the stage is idempotent.
    for (artist, album), sources in grouped.items():
        directory, paths = _publish_album_tree(
            import_root, incoming_parent, job, artist, album, sources,
        )
        final_dirs.append(directory)
        final_files.extend(paths)

    if not defer_cleanup:
        shutil.rmtree(stage, ignore_errors=True)
    _cleanup_incoming_parent(incoming_parent)
    ordered_dirs = sorted(set(final_dirs))
    return {
        "status": "published",
        "message": (
            f"Published {len(final_files)} file(s) across "
            f"{len(ordered_dirs)} album folder(s)"
        ),
        "artist": "", "album": "",
        "final_dir": ordered_dirs[0] if len(ordered_dirs) == 1 else str(import_root),
        "final_dirs": ordered_dirs,
        "files": final_files,
        "media_count": len(final_files),
        "stage_dir": str(stage),
        "needs_metadata": False,
    }


def publish_stage(temp_dir, import_root, job, artist=None, album=None,
                  *, defer_cleanup=False):
    if job.get("source") == "spotify":
        return publish_spotdl_stage(
            temp_dir, import_root, job, defer_cleanup=defer_cleanup,
        )

    analysis = analyze_stage(temp_dir, job)
    artist = clean_artist_name(artist or analysis["artist"])
    album = clean_album_name(album or analysis["album"])

    if not analysis["records"]:
        return {
            "status": "error",
            "message": "No staged audio files were found.",
            **analysis,
        }

    if not artist or not album:
        return {
            "status": "needs_metadata",
            "message": "Artist or album metadata is unresolved.",
            "artist": artist,
            "album": album,
            **analysis,
        }

    for record in analysis["records"]:
        track_artist = clean_artist_name(record.get("artist"))
        retag_media(
            record["path"],
            album=album,
            album_artist=artist,
            track_artist=track_artist,
        )

    import_root = Path(import_root)
    import_root.mkdir(parents=True, exist_ok=True)
    incoming_parent = import_root / ".incoming"
    final_dir, final_files = _publish_album_tree(
        import_root, incoming_parent, job, artist, album,
        [(Path(record["path"]), Path(record["path"]).name)
         for record in analysis["records"]],
    )
    stage = stage_dir(temp_dir, job)
    if not defer_cleanup:
        shutil.rmtree(stage, ignore_errors=True)
    _cleanup_incoming_parent(incoming_parent)

    return {
        "status": "published",
        "message": f"Published {len(final_files)} file(s) to {artist}/{album}",
        "artist": artist,
        "album": album,
        "final_dir": str(final_dir),
        "files": final_files,
        "media_count": len(final_files),
        "stage_dir": str(stage),
        "needs_metadata": False,
    }
