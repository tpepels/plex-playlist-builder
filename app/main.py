from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import os
import secrets
import time
from urllib.parse import parse_qs, urlencode
from pathlib import Path
from typing import Literal

from fastapi import Query

import yaml

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
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
from .album_export import export_album_names
from .insights import analyze
from .duplicates import find_duplicates, duplicates_tsv
from .isc_discovery import fetch_catalog, compare_library
from .playlist_covers import import_cover_zip, MAX_ZIP_BYTES
from .playlist_policies import read_policies, set_policy
from .youtube_album import lookup_album, norm, same_owned_release
from .download_audit import (read_downloader_jobs, read_disk_audit,
                             read_music_inventory, read_download_history, reconcile)
from .disk_reconciliation import reconcile_inventory
from .ytdlp_submit import AlbumQueueError, downloader_ui_url, queue_album
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


@app.get("/downloads-audit")
def download_audit_page():
    """Disk-first music and download history diagnostics."""
    page = templates.env.get_template("disk_audit.html").render(
        downloader_url=downloader_ui_url()
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/api/disk-reconciliation")
def api_disk_reconciliation(
    q: str = Query(default="", max_length=150),
    status: Literal["unindexed", "all", "missing", "partial", "indexed", "ambiguous"] = "unindexed",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
):
    """Check ALL disk album folders, even if no download job survived."""
    try:
        inventory = read_music_inventory()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        _, music = connect()
        tracks = music.searchTracks()  # fresh, no 5-minute insight cache
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not read Plex Music files.") from exc
    result = reconcile_inventory(
        inventory, tracks, query=q, status=status, offset=offset, limit=limit)
    result["plex_music_locations"] = [
        str(path) for path in (getattr(music, "locations", None) or []) if path
    ]
    return result


@app.get("/api/download-history")
def api_download_history(
    q: str = Query(default="", max_length=150),
    status: Literal["all", "failed", "incomplete", "published"] = "all",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
):
    try:
        return read_download_history(query=q, status=status,
                                     offset=offset, limit=limit)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/download-audit")
def api_download_audit(q: str = Query(default="", max_length=150)):
    """Reconcile all retained yt-dlp album jobs with actual Plex media paths."""
    try:
        jobs = read_downloader_jobs()
        disk = read_disk_audit()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        _, music = connect()
        tracks = music.searchTracks()  # A fresh Plex index, never the insights cache.
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Cannot inspect the Plex Music library.") from exc
    report = reconcile(jobs, tracks, query=q, disk=disk)
    report["plex_music_locations"] = [
        str(path) for path in (getattr(music, "locations", None) or [])
        if path
    ]
    return report


@app.post("/api/download-audit/scan")
def scan_music_for_downloads():
    """Explicitly request a Plex Music scan; completion must be checked later."""
    try:
        _, music = connect()
        music.update()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not request a Plex Music scan.") from exc
    return {"ok": True, "status": "scan_requested",
            "message": "Plex accepted a library scan request. This does not mean indexing is complete."}


@app.get("/api/library-albums.tsv")
def export_album_library():
    """Download deduplicated Plex artist/album names for music discovery."""
    try:
        _, music = connect()
        content, count = export_album_names(music.searchTracks())
        return Response(
            content=content,
            media_type="text/tab-separated-values; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="plex-albums.tsv"',
                "X-Plex-Album-Count": str(count),
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


# A signed, short-lived form token ensures that merely visiting a recommendation
# link (or a link preview fetching it) cannot create a download. For multiworker
# deployments configure the same ALBUM_ACTION_SECRET on every worker.
_ALBUM_ACTION_KEY = (os.getenv("ALBUM_ACTION_SECRET") or "").encode() or secrets.token_bytes(32)


def _album_action_token(artist: str, album: str, hour: int) -> str:
    message = f"{artist}\0{album}\0{hour}".encode("utf-8")
    return hmac.new(_ALBUM_ACTION_KEY, message, hashlib.sha256).hexdigest()


def _valid_album_action(artist: str, album: str, token: str) -> bool:
    now = int(time.time() // 3600)
    return any(hmac.compare_digest(token, _album_action_token(artist, album, hour))
               for hour in (now, now - 1))


def _owned_album(artist: str, album: str) -> bool:
    """Conservative, exact artist/title equivalence; no inference across editions."""
    _, music = connect()
    return any(
        norm(getattr(track, "grandparentTitle", "")) == norm(artist)
        and same_owned_release(artist, album, getattr(track, "parentTitle", ""))
        for track in music.searchTracks()
    )


@app.get("/open-youtube-album")
def open_youtube_album(artist: str, album: str):
    """Return an immediate, read-only page while the browser resolves the album."""
    artist, album = artist.strip(), album.strip()
    if not artist or not album or len(artist) > 200 or len(album) > 250:
        raise HTTPException(status_code=422, detail="Enter a valid artist and album title.")
    token = _album_action_token(artist, album, int(time.time() // 3600))
    search_url = "https://music.youtube.com/search?" + urlencode({"q": f"{artist} {album}"})
    page = templates.env.get_template("album_download.html").render(
        artist=artist, album=album, token=token, search_url=search_url,
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.post("/queue-youtube-album")
async def queue_youtube_album(request: Request):
    raw = await request.body()
    if len(raw) > 4096:
        raise HTTPException(status_code=413, detail="Album submission too large.")
    body = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    if any(len(body.get(key, [])) != 1 for key in ("artist", "album", "token")):
        raise HTTPException(status_code=400, detail="Incomplete album submission.")
    artist, album, token = (body[key][0] for key in ("artist", "album", "token"))
    if not _valid_album_action(artist, album, token):
        raise HTTPException(status_code=403, detail="Expired or invalid album action token.")
    resolved = await run_in_threadpool(youtube_album, artist, album)
    if resolved["status"] != "found":
        raise HTTPException(status_code=409, detail="Album is no longer unambiguous.")
    try:
        owned = await run_in_threadpool(_owned_album, artist, album)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not verify Plex library ownership.") from exc
    if owned:
        raise HTTPException(status_code=409, detail="This exact album is already in Plex.")
    audit = {
        "requested_artist": artist, "requested_album": album,
        "matched_artist": resolved["artist"], "matched_album": resolved["album"],
        "match_type": resolved["match_type"], "catalog_url": resolved["url"],
        "normalization_rules": resolved.get("normalization_rules", []),
        "queries": resolved.get("queries", []),
        "candidate_count": resolved.get("candidate_count", 0),
        "selection_policy": resolved.get("selection_policy", ""),
        "considered_candidates": resolved.get("considered_candidates", []),
    }
    try:
        job = await run_in_threadpool(queue_album, resolved["url"], audit)
    except AlbumQueueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # The download UI should only open automatically for an actual visible
    # job. A historical succeeded claim or failed job is NOT a queued item.
    active = job["visible"] and job["status"] in {
        "queued", "running", "finalizing", "needs_metadata",
    }
    target_url = downloader_ui_url() + "?" + urlencode({
        "job": job["latest_job_id"]
    })
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({
            "ok": True, "job_id": job["job_id"],
            "latest_job_id": job["latest_job_id"],
            "status": job["status"], "duplicate": job["duplicate"],
            "retried": job.get("retried", False),
            "duplicate_reason": job.get("duplicate_reason"),
            "duplicate_requests": job.get("duplicate_requests", 0),
            "visible": job["visible"], "active": active,
            "message": job["message"], "downloader_url": target_url,
        }, headers={"Cache-Control": "no-store"})
    if not active:
        return HTMLResponse(
            "<h2>Download not queued</h2><p>Existing job: "
            + html.escape(job["job_id"]) + " (" + html.escape(job["status"]) + ")</p>"
            + "<p>" + html.escape(job["message"]) + "</p>",
            status_code=409, headers={"Cache-Control": "no-store"},
        )
    return RedirectResponse(
        target_url, status_code=303,
        headers={"Cache-Control": "no-store", "X-Album-Job-Id": job["job_id"]},
    )


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
