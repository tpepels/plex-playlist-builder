from __future__ import annotations

import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

import requests
import urllib3
from .playlist_covers import restore_after_sync
from plexapi.server import PlexServer
from plexapi.exceptions import NotFound


DEFAULT_PREFS = Path(
    "/plex-config/Library/Application Support/Plex Media Server/Preferences.xml"
)

DEFAULT_HIDDEN_PLAYLIST_TITLES = {
    "All Music",
    "Recently Added",
    "Recently Played",
}


def _norm(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() not in {"0", "false", "no", "off"}


def discover_token() -> str:
    token = os.getenv("PLEX_TOKEN", "").strip()
    if token:
        return token

    prefs = Path(os.getenv("PLEX_PREFERENCES", str(DEFAULT_PREFS)))
    if not prefs.exists():
        raise RuntimeError(
            "No PLEX_TOKEN was set and Plex Preferences.xml was not found at "
            f"{prefs}. Mount /srv/plex/config read-only or set PLEX_TOKEN."
        )

    try:
        root = ET.parse(prefs).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RuntimeError(f"Could not read Plex Preferences.xml: {exc}") from exc

    token = root.attrib.get("PlexOnlineToken", "").strip()
    if not token:
        raise RuntimeError("PlexOnlineToken was not present in Preferences.xml.")
    return token


def connect():
    base_url = os.getenv("PLEX_URL", "http://host.docker.internal:32400").rstrip("/")
    token = discover_token()
    timeout = int(os.getenv("PLEX_TIMEOUT", "10"))
    verify_ssl = _env_bool("PLEX_VERIFY_SSL", True)

    session = requests.Session()
    session.verify = verify_ssl
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        plex = PlexServer(base_url, token, session=session, timeout=timeout)
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            "Could not verify Plex's HTTPS certificate. For a trusted local Plex "
            "server using host.docker.internal, set PLEX_VERIFY_SSL=false."
        ) from exc

    library_name = os.getenv("PLEX_LIBRARY", "Music")
    try:
        music = plex.library.section(library_name)
    except NotFound as exc:
        names = [section.title for section in plex.library.sections()]
        raise RuntimeError(
            f"Plex music library '{library_name}' was not found. Available libraries: {', '.join(names)}"
        ) from exc
    return plex, music


@dataclass
class Match:
    requested: dict[str, Any]
    status: str
    matched: str | None = None
    rating_key: str | None = None
    tracks: int = 0
    note: str | None = None

    def dict(self) -> dict[str, Any]:
        return asdict(self)


def _best_album(candidates: Iterable[Any], artist: str, album: str):
    a_album = _norm(album)
    a_artist = _norm(artist)
    ranked = []
    for obj in candidates:
        title = _norm(getattr(obj, "title", ""))
        parent = _norm(getattr(obj, "parentTitle", ""))
        score = 0
        if title == a_album:
            score += 100
        elif a_album and (a_album in title or title in a_album):
            score += 45
        if parent == a_artist:
            score += 100
        elif a_artist and (a_artist in parent or parent in a_artist):
            score += 35
        ranked.append((score, obj))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked or ranked[0][0] < 130:
        return None
    return ranked[0][1]


def _best_track(candidates: Iterable[Any], artist: str, track: str, album: str | None):
    n_track = _norm(track)
    n_artist = _norm(artist)
    n_album = _norm(album)
    ranked = []
    for obj in candidates:
        title = _norm(getattr(obj, "title", ""))
        grandparent = _norm(getattr(obj, "grandparentTitle", ""))
        parent = _norm(getattr(obj, "parentTitle", ""))
        score = 0
        if title == n_track:
            score += 100
        elif n_track and (n_track in title or title in n_track):
            score += 45
        if grandparent == n_artist:
            score += 100
        elif n_artist and (n_artist in grandparent or grandparent in n_artist):
            score += 35
        if n_album:
            if parent == n_album:
                score += 40
            elif n_album in parent or parent in n_album:
                score += 15
        ranked.append((score, obj))
    ranked.sort(key=lambda x: x[0], reverse=True)
    threshold = 170 if n_album else 130
    if not ranked or ranked[0][0] < threshold:
        return None
    return ranked[0][1]


def resolve_item(plex, music, item: dict[str, Any]):
    artist = str(item.get("artist", "")).strip()
    album = str(item.get("album", "")).strip()
    track = str(item.get("track", "")).strip()
    rating_key = str(item.get("rating_key", "")).strip()

    if rating_key:
        try:
            found = plex.fetchItem(int(rating_key))
        except (ValueError, NotFound):
            return [], Match(item, "missing", note=f"Track rating key {rating_key} not found")
        if getattr(found, "TYPE", "") != "track" and getattr(found, "type", "") != "track":
            return [], Match(item, "invalid", note=f"Rating key {rating_key} is not a track")
        return [found], Match(
            item,
            "matched",
            matched=f"{getattr(found, 'grandparentTitle', '')} - {getattr(found, 'parentTitle', '')} - {found.title}",
            rating_key=str(found.ratingKey),
            tracks=1,
        )

    if album and artist and not track:
        candidates = music.searchAlbums(title=album)
        found = _best_album(candidates, artist, album)
        if not found:
            candidates = music.searchAlbums()
            found = _best_album(candidates, artist, album)
        if not found:
            return [], Match(item, "missing", note="Album not found")
        tracks = found.tracks()
        return tracks, Match(
            item,
            "matched",
            matched=f"{found.parentTitle} - {found.title}",
            rating_key=str(found.ratingKey),
            tracks=len(tracks),
        )

    if track and artist:
        candidates = music.searchTracks(title=track)
        found = _best_track(candidates, artist, track, album or None)
        if not found:
            candidates = music.searchTracks()
            found = _best_track(candidates, artist, track, album or None)
        if not found:
            return [], Match(item, "missing", note="Track not found")
        return [found], Match(
            item,
            "matched",
            matched=f"{found.grandparentTitle} - {found.parentTitle} - {found.title}",
            rating_key=str(found.ratingKey),
            tracks=1,
        )

    return [], Match(
        item,
        "invalid",
        note="Each item needs rating_key, artist+album, or artist+track (album optional for tracks).",
    )


def resolve_playlist(plex, music, spec: dict[str, Any]):
    tracks = []
    matches: list[Match] = []
    seen: set[str] = set()
    for item in spec.get("items", []):
        item_tracks, match = resolve_item(plex, music, item)
        matches.append(match)
        for track in item_tracks:
            key = str(track.ratingKey)
            if key not in seen:
                seen.add(key)
                tracks.append(track)
    return tracks, matches


def _is_hidden_audio_playlist(playlist) -> bool:
    title = str(getattr(playlist, "title", "") or "").strip()
    return (
        not title
        or title in DEFAULT_HIDDEN_PLAYLIST_TITLES
        or bool(getattr(playlist, "smart", False))
        or bool(getattr(playlist, "radio", False))
    )


def hidden_audio_playlist_titles(plex) -> set[str]:
    """Titles that belong to Plex smart/radio utility playlists, not managed packs."""
    hidden = set(DEFAULT_HIDDEN_PLAYLIST_TITLES)
    for playlist in plex.playlists(playlistType="audio"):
        if _is_hidden_audio_playlist(playlist):
            title = str(getattr(playlist, "title", "") or "").strip()
            if title:
                hidden.add(title)
    return hidden


def export_audio_playlists(plex) -> dict[str, dict[str, Any]]:
    """Return user-managed Plex audio playlists as importable definitions."""
    exported: dict[str, dict[str, Any]] = {}
    playlists = sorted(
        plex.playlists(playlistType="audio"),
        key=lambda playlist: str(getattr(playlist, "title", "")).casefold(),
    )

    for playlist in playlists:
        if _is_hidden_audio_playlist(playlist):
            continue
        title = str(getattr(playlist, "title", "") or "").strip()

        items = []
        for track in playlist.items():
            rating_key = str(getattr(track, "ratingKey", "") or "").strip()
            artist = str(getattr(track, "grandparentTitle", "") or "").strip()
            album = str(getattr(track, "parentTitle", "") or "").strip()
            track_title = str(getattr(track, "title", "") or "").strip()

            # Keep the exact Plex key as the primary identity while also including
            # readable metadata so exports are useful outside this application.
            item: dict[str, Any] = {}
            if rating_key:
                item["rating_key"] = rating_key
            if artist:
                item["artist"] = artist
            if album:
                item["album"] = album
            if track_title:
                item["track"] = track_title
            if item:
                items.append(item)

        spec: dict[str, Any] = {"strict": True, "items": items}
        summary = str(getattr(playlist, "summary", "") or "").strip()
        if summary:
            spec["description"] = summary
        exported[title] = spec

    return exported


def sync_playlist(plex, music, name: str, spec: dict[str, Any]):
    """Synchronize exact track order without deleting an existing Plex playlist.

    All legacy endpoints use the same identity-preserving write and rollback
    machinery as the approved curation workflow. An ambiguous name, smart
    playlist, failed Plex mutation or unexpected order is never treated as a
    completed sync. In particular a failed create cannot remove old content.
    """
    from .curation_apply import _WRITE_LOCK, _apply_in_place, current_keys
    from .generation_publish import PUBLISH_LOCK

    tracks, matches = resolve_playlist(plex, music, spec)
    problems = [m for m in matches if m.status != "matched"]
    strict = bool(spec.get("strict", True))
    result = {
        "name": name, "strict": strict, "track_count": len(tracks),
        "matches": [m.dict() for m in matches], "changed": False,
    }
    if strict and problems:
        result.update(status="blocked",
                      message="Strict mode: missing tracks; playlist was not changed.")
        return result
    if not tracks:
        result.update(status="blocked", message="No matching tracks; playlist was not changed.")
        return result
    if name in DEFAULT_HIDDEN_PLAYLIST_TITLES:
        result.update(status="blocked", message="Protected system playlist.")
        return result

    target = [str(item.ratingKey) for item in tracks]
    if len(target) != len(set(target)):
        result.update(status="blocked",
                      message="Duplicate Plex track IDs; cannot safely synchronize.")
        return result
    # Keep the lock order consistent with collaboration imports and generation
    # publications. Refuse concurrent writes to the same playlist identity.
    with PUBLISH_LOCK, _WRITE_LOCK:
        existing = [p for p in plex.playlists(playlistType="audio")
                    if str(getattr(p, "title", "")).casefold() == name.casefold()]
        if len(existing) > 1:
            result.update(status="blocked", message="Ambiguous Plex playlist names; no changes made.")
            return result
        if existing and _is_hidden_audio_playlist(existing[0]):
            result.update(status="blocked", message="Cannot modify Plex smart/radio playlists.")
            return result
        if not existing:
            created = None
            try:
                created = plex.createPlaylist(title=name, items=tracks)
                if not getattr(created, "ratingKey", None):
                    raise RuntimeError("Plex creation returned no playlist identity.")
                if current_keys(created) != target:
                    raise RuntimeError("Plex created an unexpected track order.")
                result["cover"] = restore_after_sync(plex, created)
            except Exception as exc:
                # Only a newly created playlist may be removed by rollback.
                if created is not None and getattr(created, "ratingKey", None):
                    try:
                        created.delete()
                    except Exception as rollback_error:
                        raise RuntimeError(
                            "New-playlist creation failed and cleanup failed; manual "
                            "reconciliation required: " + str(rollback_error)
                        ) from exc
                raise RuntimeError("New-playlist creation failed; existing playlists are intact. "
                                   + str(exc)) from exc
            result.update(status="synced", changed=True,
                          playlist_id=str(created.ratingKey),
                          message="Created a new Plex playlist with verified ordering.")
            return result

        playlist = existing[0]
        if not getattr(playlist, "ratingKey", None):
            result.update(status="blocked", message="Existing Plex playlist has no stable identity.")
            return result
        before = current_keys(playlist)
        if len(before) != len(set(before)):
            result.update(status="blocked",
                          message="Existing playlist contains duplicate tracks; manual review required.")
            return result
        result["playlist_id"] = str(playlist.ratingKey)
        if before == target:
            result.update(status="synced", message="Existing Plex track order already matches.")
            return result
        objects = {str(item.ratingKey): item for item in tracks if str(item.ratingKey) not in before}
        try:
            _apply_in_place(playlist, target, objects)
        except Exception as exc:
            errors = []
            try:
                remaining = set(current_keys(playlist))
                restore = {key: plex.fetchItem(int(key))
                           for key in before if key not in remaining}
                _apply_in_place(playlist, before, restore)
            except Exception as rollback_error:
                errors.append(str(rollback_error))
            if errors:
                raise RuntimeError(
                    "Plex sync failed; rollback unsuccessful and manual reconciliation "
                    "is required: " + "; ".join(errors)
                ) from exc
            raise RuntimeError("Plex sync failed; original playlist order was restored.") from exc
        result.update(status="synced", changed=True,
                      message="Updated the original Plex playlist in place.")
        return result
