from __future__ import annotations

import csv
import io
import time
from urllib.parse import urlencode
from pathlib import Path
from typing import Literal

from fastapi import Query

import yaml

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .config_store import (
    fingerprint,
    inspect_import,
    load,
    mark_synced,
    merge_text,
    parse_text,
    pending_names,
    read_text,
    save_text,
    version_details,
)
from .recent_export import export_recent_tracks
from .insights import analyze
from .duplicates import find_duplicates, duplicates_tsv
from .isc_discovery import fetch_catalog, compare_library
from .playlist_covers import import_cover_zip, MAX_ZIP_BYTES
from .playlist_policies import read_policies, set_policy
from .youtube_album import lookup_album
from .plex_client import (
    DEFAULT_HIDDEN_PLAYLIST_TITLES,
    connect,
    export_audio_playlists,
    hidden_audio_playlist_titles,
    resolve_playlist,
    sync_playlist,
)


app = FastAPI(title="Plex Playlist Manager", version="1.5.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class ConfigBody(BaseModel):
    text: str


class ImportBody(ConfigBody):
    filename: str | None = None


class PendingSyncBody(BaseModel):
    version_id: str


class PolicyBody(BaseModel):
    kind: Literal["curated", "rotating"]


class CurateBody(BaseModel):
    playlist: str
    rating_keys: list[str]
    album_key: str
    version_id: str


# Cache raw Plex track objects briefly; recalculate playlist metrics each request
# against the latest saved YAML/policies. A manual refresh bypasses the cache.
insights_track_cache: dict = {}


def _insights_tracks(music, refresh=False):
    stamp = time.monotonic()
    if not refresh and insights_track_cache.get("tracks") is not None and stamp - insights_track_cache["at"] < 300:
        return insights_track_cache["tracks"]
    tracks = music.searchTracks()
    insights_track_cache.update({"at": stamp, "tracks": tracks})
    return tracks


def _configured_playlists():
    return load().get("playlists", {})


def _hidden_playlist_titles() -> set[str]:
    try:
        plex, _ = connect()
        return hidden_audio_playlist_titles(plex)
    except Exception:
        return set(DEFAULT_HIDDEN_PLAYLIST_TITLES)


def _config_payload(data: dict | None = None) -> dict:
    data = data or load()
    definitions = data.get("playlists", {})
    hidden = _hidden_playlist_titles()
    version = version_details(definitions)
    pending = [
        name for name in definitions
        if name not in hidden and version["sync_statuses"].get(name) == "needs_sync"
    ]

    ordered: dict = {}
    for name in pending:
        ordered[name] = definitions[name]
    for name, spec in definitions.items():
        if name not in hidden and name not in ordered:
            ordered[name] = spec

    visible_statuses = {name: version["sync_statuses"][name] for name in ordered}
    version["sync_statuses"] = visible_statuses
    version["counts"] = {
        "total": len(visible_statuses),
        "needs_sync": sum(x == "needs_sync" for x in visible_statuses.values()),
        "synced": sum(x == "synced" for x in visible_statuses.values()),
        "unverified": sum(x == "unverified" for x in visible_statuses.values()),
    }
    return {
        "playlists": list(ordered.keys()),
        "definitions": ordered,
        "pending": pending,
        "version": version,
        "hidden_playlists": sorted(name for name in definitions if name in hidden),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/health")
def health(response: Response):
    try:
        plex, music = connect()
        return {
            "ok": True,
            "server": plex.friendlyName,
            "library": music.title,
            "version": plex.version,
        }
    except Exception as exc:
        response.status_code = 503
        return {"ok": False, "error": str(exc)}


@app.get("/api/config")
def get_config():
    try:
        payload = _config_payload(load())
        payload["text"] = read_text()
        return payload
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/config")
def put_config(body: ConfigBody):
    try:
        data = save_text(body.text)
        payload = _config_payload(data)
        return {"ok": True, **payload}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/inspect-config")
def inspect_config(body: ConfigBody):
    """Preview changes in a YAML file without importing it."""
    try:
        return inspect_import(body.text)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/import-config")
def import_config(body: ImportBody):
    try:
        comparison = inspect_import(body.text)
        data = merge_text(body.text, source_filename=body.filename)
        payload = _config_payload(data)
        visible = set(payload["playlists"])
        return {
            "ok": True,
            **payload,
            "comparison": comparison,
            "changed": [name for name in comparison["new"] + comparison["updated"] if name in visible],
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/playlist-covers/import-zip")
async def import_playlist_cover_archive(request: Request):
    """Upload a ZIP of JPG/PNG covers. Files are persisted in /data before Plex calls."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("application/zip", "application/x-zip-compressed", "application/octet-stream"):
        raise HTTPException(status_code=415, detail="Please upload a ZIP archive.")
    length = request.headers.get("content-length", "0")
    if length.isdigit() and int(length) > MAX_ZIP_BYTES:
        raise HTTPException(status_code=413, detail="ZIP exceeds 70 MiB.")
    parts = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_ZIP_BYTES:
            raise HTTPException(status_code=413, detail="ZIP exceeds 70 MiB.")
        parts.append(chunk)
    try:
        try:
            plex, _ = connect()
        except Exception:
            # Keep valid covers on disk even if Plex is temporarily offline.
            plex = None
        return import_cover_zip(b"".join(parts), plex=plex)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Could not save playlist covers to persistent storage.") from exc


@app.get("/api/library-export.tsv")
def export_library():
    """Download the Plex music library as a compact track-level TSV."""
    try:
        _, music = connect()
        tracks = music.searchTracks()

        rows = []
        for track in tracks:
            rows.append(
                {
                    "artist": getattr(track, "grandparentTitle", "") or "",
                    "album": getattr(track, "parentTitle", "") or "",
                    "title": getattr(track, "title", "") or "",
                    "disc": getattr(track, "parentIndex", "") or "",
                    "track": getattr(track, "index", "") or "",
                    "year": getattr(track, "year", "") or "",
                    "duration_ms": getattr(track, "duration", "") or "",
                    "rating_key": str(getattr(track, "ratingKey", "") or ""),
                }
            )

        rows.sort(
            key=lambda row: (
                str(row["artist"]).casefold(),
                str(row["album"]).casefold(),
                int(row["disc"]) if str(row["disc"]).isdigit() else 0,
                int(row["track"]) if str(row["track"]).isdigit() else 0,
                str(row["title"]).casefold(),
            )
        )

        out = io.StringIO()
        fieldnames = [
            "artist",
            "album",
            "title",
            "disc",
            "track",
            "year",
            "duration_ms",
            "rating_key",
        ]
        writer = csv.DictWriter(out, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

        return Response(
            content=out.getvalue(),
            media_type="text/tab-separated-values; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="plex-music-library.tsv"',
                "X-Plex-Track-Count": str(len(rows)),
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/library-recent.tsv")
def export_recent_library(
    mode: Literal["hours", "albums"] = "hours",
    limit: int = Query(default=48, ge=1, le=8760),
):
    """Download recently added tracks or complete recently added albums as TSV."""
    if mode == "albums" and limit > 200:
        raise HTTPException(status_code=422, detail="Choose between 1 and 200 albums.")
    try:
        _, music = connect()
        content, track_count, album_count = export_recent_tracks(music, mode=mode, limit=limit)
        suffix = f"{limit}h" if mode == "hours" else f"{limit}-albums"
        return Response(
            content=content,
            media_type="text/tab-separated-values; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="plex-recent-{suffix}.tsv"',
                "X-Plex-Track-Count": str(track_count),
                "X-Plex-Album-Count": str(album_count),
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/playlists-export.yml")
def export_playlists():
    """Download all Plex audio playlists as playlist-builder YAML."""
    try:
        plex, _ = connect()
        playlists = export_audio_playlists(plex)
        track_count = sum(len(spec.get("items", [])) for spec in playlists.values())
        rendered = yaml.safe_dump(
            {"playlists": playlists},
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        return Response(
            content=rendered,
            media_type="application/yaml; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="plex-playlists.yml"',
                "X-Plex-Playlist-Count": str(len(playlists)),
                "X-Plex-Track-Count": str(track_count),
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/discover/isc")
def discover_isc(refresh: bool = False):
    """Curated records, cross-referenced against Plex. Never alters playlists."""
    try:
        # Don't contact Plex until we have (cached or fetched) source records.
        catalog = fetch_catalog(refresh=refresh)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        _, music = connect()
        return compare_library(catalog, _insights_tracks(music, refresh=refresh))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Plex library is unavailable for discovery comparison.") from exc


@app.get("/api/duplicates")
def library_duplicates(refresh: bool = False):
    """Inspect duplicate-looking Plex music tracks without changing files."""
    try:
        _, music = connect()
        return find_duplicates(_insights_tracks(music, refresh=refresh))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/duplicates.tsv")
def library_duplicates_export(refresh: bool = False):
    """Download the complete duplicates review, including Plex rating keys."""
    try:
        _, music = connect()
        report = find_duplicates(_insights_tracks(music, refresh=refresh))
        return Response(
            content=duplicates_tsv(report),
            media_type="text/tab-separated-values; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="plex-library-duplicates.tsv"',
                "X-Plex-Duplicate-Groups": str(len(report["groups"])),
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/insights")
def insights(
    recent_days: int = Query(default=30, ge=1, le=365),
    refresh: bool = False,
):
    """Analyze saved playlist definitions against the current Plex library."""
    try:
        _, music = connect()
        return analyze(
            _insights_tracks(music, refresh=refresh),
            _configured_playlists(),
            policies=read_policies(),
            recent_days=recent_days,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.put("/api/playlist-policy/{name}")
def update_playlist_policy(name: str, body: PolicyBody):
    """Update advisory rotation mode without modifying playlist YAML or Plex."""
    playlists = _configured_playlists()
    if name not in playlists:
        raise HTTPException(status_code=404, detail="Unknown playlist")
    try:
        return {"name": name, "kind": set_policy(name, body.kind, playlists.keys())}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/curate-tracks")
def curate_tracks(body: CurateBody):
    """Explicitly add selected existing Plex tracks to one chosen playlist."""
    definitions = _configured_playlists()
    if fingerprint(definitions) != body.version_id:
        raise HTTPException(status_code=409, detail="Playlist definitions changed. Reload first.")
    if body.playlist not in definitions:
        raise HTTPException(status_code=404, detail="Unknown playlist")
    if not body.rating_keys or len(body.rating_keys) > 50:
        raise HTTPException(status_code=422, detail="Select 1–50 tracks.")
    keys = list(dict.fromkeys(str(x).strip() for x in body.rating_keys))
    _, music = connect()
    from .insights import track_model
    available = {item["key"]: item for item in
                 (track_model(t) for t in _insights_tracks(music))}
    if any(key not in available or available[key]["album_key"] != body.album_key for key in keys):
        raise HTTPException(status_code=422, detail="Selected tracks no longer match the album in Plex.")

    # Existing track entries are not duplicated, and no other playlist changes.
    playlist = definitions[body.playlist]
    from .insights import normalize
    existing = {str(item.get("rating_key", "")).strip()
                for item in playlist.get("items", [])}
    for item in playlist.get("items", []):
        if item.get("rating_key"):
            continue
        a, al, title = (normalize(item.get("artist")),
                        normalize(item.get("album")),
                        normalize(item.get("track")))
        for key in keys:
            candidate = available[key]
            if a == normalize(candidate["artist"]) and (not al or al == normalize(candidate["album"])):
                # Album-level item covers every track, track-level item only
                # covers the named recording.
                if not title or title == normalize(candidate["title"]):
                    existing.add(key)
    added = [key for key in keys if key not in existing]
    if not added:
        return {"added": 0, "status": "unchanged"}
    updated = {**definitions}
    updated_spec = dict(playlist)
    updated_spec["items"] = list(playlist.get("items", [])) + [
        {"rating_key": key, "artist": available[key]["artist"],
         "album": available[key]["album"], "track": available[key]["title"]}
        for key in added
    ]
    updated[body.playlist] = updated_spec
    data = load()
    data["playlists"] = updated
    save_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    # Save is durable even if the subsequent Plex sync fails; status stays pending.
    return {"added": len(added), "status": "needs_sync", "playlist": body.playlist}


@app.get("/open-youtube-album")
def open_youtube_album(artist: str, album: str):
    """Open a verified album, or fall back to an artist + album search."""
    resolved = youtube_album(artist, album)
    if resolved["status"] != "found":
        query = f"{artist.strip()} {album.strip()}"
        search_url = "https://music.youtube.com/search?" + urlencode({"q": query})
        return RedirectResponse(search_url, status_code=302)
    return RedirectResponse(resolved["url"], status_code=302)


@app.get("/api/youtube-album")
def youtube_album(artist: str, album: str):
    """Return an exact matching YouTube Music album link or an explicit not-found."""
    try:
        return lookup_album(artist, album)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        # Never substitute a guessed URL after an upstream error.
        raise HTTPException(status_code=503, detail="YouTube Music lookup unavailable.") from exc


@app.get("/api/preview/{name}")
def preview(name: str):
    try:
        playlists = _configured_playlists()
        if name not in playlists:
            raise HTTPException(status_code=404, detail="Unknown playlist")
        plex, music = connect()
        tracks, matches = resolve_playlist(plex, music, playlists[name])
        return {
            "name": name,
            "server": plex.friendlyName,
            "track_count": len(tracks),
            "matches": [m.dict() for m in matches],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/sync/{name}")
def sync(name: str):
    try:
        playlists = _configured_playlists()
        if name not in playlists:
            raise HTTPException(status_code=404, detail="Unknown playlist")
        if name in _hidden_playlist_titles():
            raise HTTPException(status_code=400, detail="Plex utility playlists are not managed here.")
        plex, music = connect()
        result = sync_playlist(plex, music, name, playlists[name])
        if result.get("status") == "synced":
            mark_synced(name, playlists[name])
        result["pending"] = name in pending_names(playlists.keys())
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/sync-pending/{name}")
def sync_pending(name: str, body: PendingSyncBody):
    """Sync one pending playlist from a bulk run, without touching unchanged ones.

    The browser runs these calls sequentially to provide progress and resumable
    errors rather than one long request that may time out for large imports.
    """
    try:
        playlists = _configured_playlists()
        if fingerprint(playlists) != body.version_id:
            raise HTTPException(
                status_code=409,
                detail="The loaded playlist version changed. Reload before continuing bulk sync.",
            )
        if name not in playlists:
            raise HTTPException(status_code=404, detail="Unknown playlist")

        spec = playlists[name]
        if not spec.get("enabled", True):
            return {"name": name, "status": "skipped", "message": "Playlist is disabled."}
        if version_details(playlists)["sync_statuses"][name] != "needs_sync":
            return {"name": name, "status": "skipped", "message": "Playlist is no longer pending."}

        plex, music = connect()
        if name in hidden_audio_playlist_titles(plex):
            return {"name": name, "status": "skipped", "message": "Plex utility playlists are excluded."}

        result = sync_playlist(plex, music, name, spec)
        if result.get("status") == "synced":
            mark_synced(name, spec)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/sync-all")
def sync_all():
    try:
        playlists = _configured_playlists()
        plex, music = connect()
        hidden = hidden_audio_playlist_titles(plex)
        results = []
        for name, spec in playlists.items():
            if name in hidden or not spec.get("enabled", True):
                continue
            result = sync_playlist(plex, music, name, spec)
            if result.get("status") == "synced":
                mark_synced(name, playlists[name])
            results.append(result)
        return {"results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
