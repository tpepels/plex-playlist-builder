from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import html
import io
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode
from pathlib import Path
from typing import Literal

from fastapi import Query

import yaml
import httpx

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
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
from .audiomuse_verification import verify_plex_track_keys
from .audiomuse_client import (cached_features, synchronize as synchronize_audiomuse,
                              readiness as audiomuse_readiness, feature_revision)
from .audiomuse_coherence import coherence_report
from .audiomuse_analysis import AudioMuseControlError, analysis_status, start_analysis
from .playlist_curator import (
    CurationConflict, assess, candidates as curation_candidates, catalogue,
    resolve_items, signature, compute_operations,
)
from .curation_apply import find_live, current_items, current_keys, change_plan, apply_approved
from .curation_library import get_library, metadata as curation_library_metadata
from .playlist_generation import (parse_prompt, generate as generate_playlist,
                                  diagnose as generation_diagnose, GenerationError,
                                  _constraints, genre_profiles, genre_evidence,
                                  catalogue_revision, GENRE_ALIASES, SUBGENRE_ALIASES,
                                  _bounded_ids, SELECTION_POLICY_VERSION)
from .generation_store import (DraftConflict, create as create_generation_draft,
                               get as get_generation_draft, revise as revise_generation_draft,
                               genre_feedback as generation_genre_feedback)
from .generation_publish import prepare as prepare_generation_publish, publish as publish_generation_draft
from .playlist_identity import (
    IdentityConflict, identity as playlist_identity, get_override,
    put_override, delete_override, revision as identity_revision,
)
from .playlist_ranking import rank_candidates
from .curation_impact import compare as compare_curations
from .curation_validation import (
    ISSUES as VALIDATION_ISSUES,
    categories as validation_categories, portfolio as validation_portfolio,
    summary as validation_summary,
    reviews as validation_reviews, record as record_validation,
    calibration as validation_calibration, feedback_for as validation_feedback,
)
from fastapi.responses import StreamingResponse
from .duplicates import find_duplicates, duplicates_tsv
from .isc_discovery import fetch_catalog, compare_library
from .playlist_covers import import_cover_zip, MAX_ZIP_BYTES
from .playlist_policies import read_policies, set_policy
from .youtube_album import lookup_album, norm, same_owned_release
from .download_audit import (read_downloader_jobs, read_disk_audit,
                             read_music_inventory, read_download_history, reconcile)
from .disk_reconciliation import reconcile_inventory
from .ytdlp_submit import AlbumQueueError, downloader_ui_url, queue_album
from .downloader_bridge import router as downloader_router
from .download_page import DownloadPageError, fetch_native_page
from . import download_plex_sync, background_maintenance
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
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(downloader_router)


@app.on_event("startup")
async def start_downloader_library_sync():
    background_maintenance.start_tasks(app)
    if os.getenv("DOWNLOAD_BRIDGE_SCAN_ENABLED", "0") == "1":
        app.state.downloader_sync_task = asyncio.create_task(
            download_plex_sync.sync_loop(), name="download-plex-sync",
        )


@app.on_event("shutdown")
async def stop_downloader_library_sync():
    await background_maintenance.stop_tasks(app)
    task = getattr(app.state, "downloader_sync_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass



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


class VerifyPlexTrackKeysBody(BaseModel):
    rating_keys: list[str]

class StartAudioMuseAnalysisBody(BaseModel):
    recent_albums: int
    confirm_full_library: bool = False




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
    """Read the saved YAML without depending on Plex availability.

    The dynamically discovered Plex smart/radio playlists are checked at the
    actual sync boundary. A network failure must never block local definitions,
    imports, or the playlist editor.
    """
    data = load() if data is None else data
    definitions = data.get("playlists", {})
    hidden = set(DEFAULT_HIDDEN_PLAYLIST_TITLES)
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


DASHBOARD_VIEWS = frozenset({
    "playlists", "health", "duplicates", "newmusic", "isc", "discover"
})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Primary landing page, retaining old /?view=... bookmarked sections."""
    if request.query_params.get("view") in DASHBOARD_VIEWS:
        return templates.TemplateResponse("index.html", {"request": request})
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/playlists", response_class=HTMLResponse)
def playlist_workspace(request: Request):
    """The full playlist management workspace, unchanged for saved data."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/workspace/maintenance")
def workspace_maintenance():
    """Read-only status for the automatic local cache updaters."""
    return background_maintenance.snapshot()


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


@app.get("/api/config/status")
def config_status():
    """Small read-only revision check for automatic playlist-view updates."""
    try:
        version = _config_payload(load())["version"]
        return {"version": version}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Playlist state unavailable.") from exc


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


@app.post("/api/plex/verify-rating-keys")
def verify_rating_keys(body: VerifyPlexTrackKeysBody):
    """Bounded read-only ID check; does not export the entire Plex library."""
    if not 1 <= len(body.rating_keys) <= 25:
        raise HTTPException(status_code=422, detail="Supply 1-25 track IDs.")
    try:
        plex, _ = connect()
        return verify_plex_track_keys(plex, body.rating_keys)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Plex track verification unavailable") from exc


@app.get("/api/audiomuse/analysis/status")
def get_audiomuse_analysis_status():
    """Read AudioMuse's active and last main-task state."""
    try:
        return analysis_status()
    except (AudioMuseControlError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/audiomuse/analysis/start")
def start_audiomuse_analysis(body: StartAudioMuseAnalysisBody, request: Request):
    """Queue an AudioMuse analysis; never trigger clustering or playlist changes."""
    # This app is LAN-accessible. Deny cross-origin browser POSTs to this endpoint.
    # A direct local caller can send an Origin header; the browser UI does so.
    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Same-origin browser request required.")
    try:
        return start_analysis(
            recent_albums=body.recent_albums,
            confirm_full_library=body.confirm_full_library,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AudioMuseControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/musical-coherence", response_class=HTMLResponse)
def musical_coherence_page(request: Request):
    """AudioMuse review page; no playlist or Plex mutations."""
    return templates.TemplateResponse("musical_coherence.html", {"request": request})


@app.get("/api/audiomuse/coherence")
def audiomuse_coherence():
    """Read the local feature cache and YAML only; never query Plex."""
    try:
        features, state = cached_features()
        definitions = _configured_playlists()
        thematic = {
            name: spec for name, spec in definitions.items()
            if name not in DEFAULT_HIDDEN_PLAYLIST_TITLES
        }
        report = coherence_report(thematic, features, state)
        report["summary"]["excluded_system_playlists"] = len(definitions) - len(thematic)
        return report
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="AudioMuse cache unavailable") from exc


@app.post("/api/audiomuse/sync")
async def audiomuse_sync():
    """Sync audio analysis data to SQLite, not Plex. No playlists are modified."""
    try:
        return await run_in_threadpool(synchronize_audiomuse)
    except (RuntimeError, ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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


@app.get("/downloads", response_class=HTMLResponse)
async def unified_downloads(request: Request, job: str = Query(default="", max_length=128)):
    """Full native downloader page with Playlist Manager navigation and controls."""
    if job and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job):
        raise HTTPException(status_code=422, detail="Invalid download job ID")
    try:
        markup = await fetch_native_page(request)
    except (httpx.RequestError, DownloadPageError, OSError, RuntimeError):
        # Navigation stays available even when the independent downloader fails.
        return templates.TemplateResponse(
            "download_portal.html", {"request": request},
            headers={"Cache-Control": "no-store"},
        )
    return HTMLResponse(markup, headers={"Cache-Control": "no-store"})


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


# Playlist Intelligence: explicit human approval and preservation of Plex playlist IDs.
class CurationPlanBody(BaseModel):
    name: str
    version: str
    snapshot: str
    operations: list[dict]
    identity_revision: str | None = None


class ValidationBody(BaseModel):
    name: str
    target_id: str | None = None
    candidate_id: str
    verdict: Literal["accept", "reject"]
    issue: Literal["none","genre","energy","mood","flow","variety","recording","metadata","other"]
    note: str = ""
    listened: bool = False
    candidate_played_seconds: float = 0
    original_played_seconds: float = 0
    identity_revision: str
    snapshot: str


class IdentityUpdateBody(BaseModel):
    values: dict
    revision: str


class IdentityResetBody(BaseModel):
    revision: str


class CurationApplyBody(CurationPlanBody):
    plan_id: str
    approved: bool = False


def _curation_context(name: str, *, require_live: bool = True):
    definitions = _configured_playlists()
    if name in DEFAULT_HIDDEN_PLAYLIST_TITLES or name not in definitions:
        raise HTTPException(status_code=404, detail="Unknown curated playlist.")
    plex, music = connect()
    live = find_live(plex, name)
    library = get_library(music)
    before = current_keys(live)
    if not before or len(set(before)) != len(before):
        raise CurationConflict("Playlist is empty or contains duplicate Plex tracks.")
    if any(key not in library for key in before):
        raise CurationConflict("Playlist contains tracks absent from the configured Plex music library.")
    return plex, music, library, definitions, live, before


@app.get("/playlist-intelligence", response_class=HTMLResponse)
def playlist_intelligence_page(request: Request):
    return templates.TemplateResponse("playlist_intelligence.html", {"request": request})


@app.post("/api/curation/library/refresh")
def refresh_curation_catalogue(request: Request):
    """Explicitly rescan the full Plex library; existing snapshot remains on failure."""
    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Same-origin browser request required.")
    try:
        _, music = connect()
        library = get_library(music, refresh=True)
        return {"tracks":len(library),"state":"fresh"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Plex catalogue refresh failed; previous snapshot is retained.") from exc



# Create-only playlist generation: all edits stay in a durable review draft.
class GenerationCreateBody(BaseModel):
    prompt: str = ""  # Optional when structured genre controls are supplied.
    name: str
    count: int | None = None
    minutes: int | None = None
    identity: dict | None = None
    genre: str | None = None
    subgenre: str | None = None
    reference_track_ids: list[str] = []
    negative_reference_track_ids: list[str] = []
    excluded_track_ids: list[str] = []
    excluded_artists: list[str] = []
    excluded_albums: list[str] = []
    year_from: int | None = None
    year_to: int | None = None
    evidence_threshold: Literal["strict","balanced","exploratory"] = "balanced"


class GenerationEditBody(BaseModel):
    revision: int
    operations: list[dict]


class GenerationRegenerateBody(BaseModel):
    revision: int


class GenerationReviewBody(BaseModel):
    revision: int
    candidate_id: str
    target_id: str | None = None
    verdict: Literal["accept", "reject", "needs_review"]
    issue: str = "none"
    note: str = ""
    listened: bool = False
    candidate_played_seconds: int = 0
    original_played_seconds: int = 0


class GenerationPlanBody(BaseModel):
    draft_id: str
    revision: int
    name: str


class GenerationPublishBody(GenerationPlanBody):
    plan_id: str
    approved: bool = False


def _generation_context():
    plex,music=connect()
    return plex,get_library(music)


def _generation_payload(draft,library):
    data=draft["data"]
    keys=data["keys"]
    evidence=data.get("genre_evidence") or {}
    reviews=data.get("reviews") or {}
    rows=[{"id":k,"artist":library[k]["artist"],"album":library[k]["album"],
           "title":library[k]["title"],"year":library[k].get("year"),
           "duration_ms":library[k].get("duration_ms"),
           "genres":library[k].get("genres",[]),
           "audio_analyzed": bool(evidence.get(k,{}).get("evidence",{}).get("audio_coverage")),
           "genre_evidence": evidence.get(k),
           "review":reviews.get(k)} for k in keys]
    original_sources=data.get("source_revisions") or {}
    stale_library=bool(original_sources.get("library") and
                       original_sources["library"]!=catalogue_revision(library))
    return {**draft,"source_stale":{"library":stale_library},
            "data":{**data,"tracks":rows}}


def _generation_origin(request):
    origin=request.headers.get("origin")
    if not origin or origin.rstrip("/")!=str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403,detail="Same-origin browser request required.")


@app.get("/api/generation/readiness")
def generation_readiness():
    """Local ID/embedding reconciliation plus optional current upstream task."""
    try:
        _, music = connect()
        library = get_library(music)
        cache, state = cached_features()
        report = audiomuse_readiness(library, cache, state)
        try:
            report["analysis"] = analysis_status()
        except (AudioMuseControlError, RuntimeError):
            report["analysis"] = {"available": False, "warning": "Upstream status unavailable"}
        return report
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Plex/AudioMuse readiness unavailable") from exc


@app.get("/api/generation/genre-profiles")
def generation_genre_profiles(genre: str = "jazz", subgenre: str = "hard bop"):
    try:
        _, library = _generation_context()
        cache, state = cached_features()
        from .playlist_curator import features_for
        decoded = features_for(cache)
        from .playlist_generation import _style_token, _tags
        broad = _style_token(genre) if genre else ""
        specific = _style_token(subgenre) if subgenre else ""
        if specific and (specific not in SUBGENRE_ALIASES or broad!="jazz"):
            raise HTTPException(status_code=422,detail="Unsupported genre/subgenre pairing.")
        profiles = []
        for name in ([specific] if specific else
                     (sorted(SUBGENRE_ALIASES) if broad=="jazz" else [""])):
            target=name or broad
            candidates=[key for key,info in library.items() if target in _tags(info)]
            measured=[key for key in candidates if
                      key in decoded and any(decoded[key].get(f) is not None
                                              for f in ("musicnn","clap"))]
            profiles.append({"genre":broad,"subgenre":name,
                "verified_metadata_count":len(candidates),
                "analyzed_references":len(measured),
                "available":len(measured)>=2,
                "note":"Explicit Plex tags are used as seed labels; not an automated genre probability."})
        return {"genres":sorted(GENRE_ALIASES),
                "subgenres":sorted(SUBGENRE_ALIASES),
                "profiles":profiles, "cache_last_sync":state.get("last_sync")}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Genre profiles unavailable") from exc


@app.get("/api/generation/reference-search")
def generation_reference_search(q: str = Query(min_length=2, max_length=120),
                                limit: int = Query(default=20, ge=1, le=50)):
    """Search Plex track and album references; never download or mutate music."""
    _, library = _generation_context()
    from .insights import normalize
    needle = normalize(q)
    result=[]
    for key,info in sorted(library.items(), key=lambda p:(
            normalize(p[1]["artist"]),normalize(p[1]["album"]),
            normalize(p[1]["title"]),p[0])):
        if needle not in normalize(" ".join((
                info["artist"],info["album"],info["title"]))):
            continue
        result.append({"id":key,"artist":info["artist"],
                       "album":info["album"],"title":info["title"],
                       "genres":info.get("genres",[])})
        if len(result)>=limit:break
    albums={}
    for info in result:
        key=library[info["id"]]["album_key"]
        if key not in albums:
            albums[key]=[k for k,row in library.items() if row["album_key"]==key]
        info["album_track_ids"]=sorted(albums[key],key=lambda k:(
            library[k].get("disc") or 0,library[k].get("index") or 0,k))
    return {"tracks":result,"total_returned":len(result)}


@app.get("/playlist-generator",response_class=HTMLResponse)
def playlist_generator_page(request: Request):
    return templates.TemplateResponse("playlist_generator.html",{"request":request})


@app.post("/api/generation/drafts")
def create_generation(body: GenerationCreateBody,request: Request):
    _generation_origin(request)
    try:
        intent=parse_prompt(body.prompt,count=body.count,minutes=body.minutes,
                            overrides=body.identity,genre=body.genre,
                            subgenre=body.subgenre,
                            reference_track_ids=body.reference_track_ids,
                            negative_reference_track_ids=body.negative_reference_track_ids,
                            excluded_track_ids=body.excluded_track_ids,
                            excluded_artists=body.excluded_artists,
                            excluded_albums=body.excluded_albums,
                            year_from=body.year_from,year_to=body.year_to,
                            evidence_threshold=body.evidence_threshold)
        _,library=_generation_context()
        cached,_=cached_features()
        criteria=intent["criteria"]
        previous=generation_genre_feedback(criteria.get("genre",""),
                                          criteria.get("subgenre",""))
        built=generate_playlist(intent,library,cached,reviews=previous)
        from .generation_publish import _name
        title=_name(body.name)
        data={"request":intent,"name":title,"keys":built["keys"],
              "diagnostics":built["diagnostics"],
              "selection":built["selection"],"warnings":built["warnings"],
              "genre_evidence":built["genre_evidence"],
              "genre_profile":built["genre_profile"],"reviews":{},
              "selection_policy_version":SELECTION_POLICY_VERSION,
              "source_revisions":{
                  "library":catalogue_revision(library),
                  "audio":feature_revision(cached)
              }}
        return _generation_payload(create_generation_draft(data),library)
    except (GenerationError,ValueError) as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc


@app.get("/api/generation/drafts/{draft_id}")
def get_generation(draft_id: str):
    try:
        draft=get_generation_draft(draft_id)
        _,library=_generation_context()
        return _generation_payload(draft,library)
    except (DraftConflict,GenerationError) as exc:
        raise HTTPException(status_code=404,detail=str(exc)) from exc


@app.post("/api/generation/drafts/{draft_id}/edit")
def edit_generation(draft_id: str,body: GenerationEditBody,request: Request):
    _generation_origin(request)
    try:
        draft=get_generation_draft(draft_id)
        if draft["status"]!="draft" or draft["revision"]!=body.revision:
            raise DraftConflict("Draft was changed or already published.")
        _,library=_generation_context()
        after=compute_operations(draft["data"]["keys"],body.operations,library)
        if len(after)>120:
            raise GenerationError("A generated playlist can contain at most 120 tracks.")
        for key in after:
            if key not in library:raise DraftConflict("Track no longer exists in Plex.")
        data=dict(draft["data"])
        cached,_=cached_features()
        original_sources=data.get("source_revisions") or {}
        if original_sources and (
            original_sources.get("library")!=catalogue_revision(library) or
            original_sources.get("audio")!=feature_revision(cached)):
            raise DraftConflict("Draft evidence is stale. Regenerate it explicitly before editing.")
        data["keys"]=after
        data["diagnostics"]=generation_diagnose(after,library,cached,data["request"]["identity"],
                                                 data["request"])
        criteria=data["request"]["criteria"]
        if criteria.get("genre"):
            from .playlist_curator import features_for
            decoded=features_for(cached)
            profile=genre_profiles(library,decoded,genre=criteria["genre"],
                subgenre=criteria.get("subgenre") or "",
                reference_track_ids=criteria.get("reference_track_ids",[]),
                negative_reference_track_ids=criteria.get("negative_reference_track_ids",[]),
                evidence_threshold=criteria.get("evidence_threshold","balanced"))
            data["genre_profile"]=profile
            data["genre_evidence"]={
                key: genre_evidence(key,library[key],decoded,profile,
                                    reviews=data.get("reviews"))
                for key in after
            }
        data["source_revisions"]={
            "library":catalogue_revision(library),"audio":feature_revision(cached)
        }
        violations={key:_constraints(library[key],data["request"])
                    for key in after if _constraints(library[key],data["request"])}
        for key,signal in data.get("genre_evidence",{}).items():
            if key in after and not signal["compatible"]:
                violations[key]="outside_requested_genre"
        data["warnings"]=list(dict.fromkeys(
            [w for w in data["warnings"] if not w.startswith("Manual edits")]
            + ([f"Manual edits include {len(violations)} tracks outside the original criteria."]
               if violations else [])
        ))
        data["manual_violations"]=violations
        return _generation_payload(revise_generation_draft(draft_id,body.revision,data),
                                   library)
    except (DraftConflict,GenerationError,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@app.post("/api/generation/drafts/{draft_id}/regenerate")
def regenerate_generation(draft_id: str, body: GenerationRegenerateBody, request: Request):
    """Explicit replacement of candidate set, retaining the same draft identity."""
    _generation_origin(request)
    try:
        draft=get_generation_draft(draft_id)
        if draft["status"]!="draft" or draft["revision"]!=body.revision:
            raise DraftConflict("Draft changed. Reload before regenerating.")
        _,library=_generation_context()
        cached,_=cached_features()
        data=dict(draft["data"])
        request_spec=dict(data["request"])
        criteria=dict(request_spec["criteria"])
        rejected={key for key,row in data.get("reviews",{}).items()
                  if row.get("verdict")=="reject"}
        criteria["excluded_track_ids"]=list(dict.fromkeys(
            list(criteria.get("excluded_track_ids",[]))+sorted(rejected)))
        request_spec["criteria"]=criteria
        prior=generation_genre_feedback(criteria.get("genre",""),
                                        criteria.get("subgenre",""))
        prior.update(data.get("reviews") or {})
        generated=generate_playlist(request_spec,library,cached,reviews=prior)
        data.update({
            "request":request_spec,"keys":generated["keys"],
            "diagnostics":generated["diagnostics"],
            "warnings":generated["warnings"],
            "selection":generated["selection"],
            "genre_profile":generated["genre_profile"],
            "genre_evidence":generated["genre_evidence"],
            "source_revisions":{"library":catalogue_revision(library),
                                "audio":feature_revision(cached)},
            "selection_policy_version":SELECTION_POLICY_VERSION,
        })
        return _generation_payload(
            revise_generation_draft(draft_id,body.revision,data),library)
    except (DraftConflict,GenerationError,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@app.get("/api/generation/drafts/{draft_id}/candidates")
def generation_candidates(draft_id: str,target: str="",q: str="",
                          limit: int=Query(default=20,ge=1,le=30)):
    try:
        if len(q)>120:raise GenerationError("Search query too long.")
        draft=get_generation_draft(draft_id)
        _,library=_generation_context()
        cache,_=cached_features()
        keys=draft["data"]["keys"]
        criteria=draft["data"]["request"]["criteria"]
        signals=None
        combined_reviews=generation_genre_feedback(criteria.get("genre",""),
                                                   criteria.get("subgenre",""))
        combined_reviews.update(draft["data"].get("reviews",{}))
        if criteria.get("genre"):
            from .playlist_curator import features_for
            ff=features_for(cache)
            profile=genre_profiles(library,ff,genre=criteria["genre"],
                subgenre=criteria.get("subgenre") or "",
                reference_track_ids=criteria.get("reference_track_ids",[]),
                negative_reference_track_ids=criteria.get("negative_reference_track_ids",[]),
                evidence_threshold=criteria.get("evidence_threshold","balanced"))
            signals={
                key:genre_evidence(key,info,ff,profile,
                                    reviews=combined_reviews)
                for key,info in library.items()
                if key not in keys
            }
        rejected={key for key,review in combined_reviews.items()
                  if review.get("verdict")=="reject" and (
                      review.get("issue")=="genre" or
                      key in draft["data"].get("reviews",{}))}
        recommendations=rank_candidates(keys,library,cache,
                    draft["data"]["request"]["identity"],target=target or None,
                    query=q,limit=limit,genre_signals=signals,rejected_ids=rejected)
        for row in recommendations:
            row["criteria_issue"]=_constraints(library[row["id"]],
                                                draft["data"]["request"])
        return {"candidates":recommendations,"target":target or None,
                "revision":draft["revision"]}
    except (DraftConflict,GenerationError,CurationConflict) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@app.post("/api/generation/drafts/{draft_id}/reviews")
def generation_review(draft_id: str, body: GenerationReviewBody, request: Request):
    """Persist audited verdicts in the existing versioned generation draft store."""
    _generation_origin(request)
    try:
        draft=get_generation_draft(draft_id)
        if draft["status"]!="draft" or draft["revision"]!=body.revision:
            raise DraftConflict("Draft changed. Reload before reviewing.")
        _,library=_generation_context()
        candidate=str(body.candidate_id)
        target=str(body.target_id) if body.target_id else None
        if candidate not in library or (target and target not in draft["data"]["keys"]):
            raise DraftConflict("Review references an unknown Plex track or draft target.")
        if not isinstance(body.note,str) or len(body.note)>2000:
            raise DraftConflict("Review note exceeds 2000 characters.")
        if body.issue not in VALIDATION_ISSUES:
            raise DraftConflict("Unsupported review issue.")
        if not 0<=body.candidate_played_seconds<=7200 or not 0<=body.original_played_seconds<=7200:
            raise DraftConflict("Invalid listening duration.")
        if body.verdict in ("accept","reject") and (
                not body.listened or body.candidate_played_seconds<15 or
                (target and body.original_played_seconds<15)):
            raise DraftConflict("Review requires listening to both tracks for at least 15 seconds.")
        data=dict(draft["data"])
        source=data.get("source_revisions") or {}
        if source.get("library") and source["library"]!=catalogue_revision(library):
            raise DraftConflict("Catalogue changed since generation. Regenerate before reviewing.")
        cache,_=cached_features()
        if source.get("audio") and source["audio"]!=feature_revision(cache):
            raise DraftConflict("AudioMuse changed since generation. Regenerate before reviewing.")
        review_evidence=None
        criteria=data["request"]["criteria"]
        if criteria.get("genre"):
            from .playlist_curator import features_for
            decoded=features_for(cache)
            profile=genre_profiles(library,decoded,
                genre=criteria["genre"],subgenre=criteria.get("subgenre") or "",
                reference_track_ids=criteria.get("reference_track_ids",[]),
                negative_reference_track_ids=criteria.get("negative_reference_track_ids",[]),
                evidence_threshold=criteria.get("evidence_threshold","balanced"))
            review_evidence=genre_evidence(candidate,library[candidate],decoded,profile)
        reviews=dict(data.get("reviews") or {})
        reviews[candidate]={
            "verdict":body.verdict,"issue":body.issue,"note":body.note,
            "target_id":target, "listened":body.listened,
            "candidate_played_seconds":body.candidate_played_seconds,
            "original_played_seconds":body.original_played_seconds,
            "evidence_at_review":review_evidence,
        }
        data["reviews"]=reviews
        if candidate in data.get("genre_evidence",{}):
            current=dict(data["genre_evidence"])
            current[candidate]={**current[candidate],
                "evidence":{**current[candidate]["evidence"],
                            "review_status":body.verdict}}
            data["genre_evidence"]=current
        return _generation_payload(revise_generation_draft(draft_id,body.revision,data),library)
    except (DraftConflict,GenerationError,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@app.post("/api/generation/plan")
def generation_plan(body: GenerationPlanBody):
    try:
        plex,library=_generation_context()
        draft=get_generation_draft(body.draft_id)
        if draft["revision"]!=body.revision:
            raise DraftConflict("Draft changed. Reload before reviewing.")
        source=draft["data"].get("source_revisions") or {}
        if source.get("audio") and source["audio"]!=feature_revision(cached_features()[0]):
            raise DraftConflict("AudioMuse evidence changed. Regenerate the draft before publishing.")
        return prepare_generation_publish(plex,draft,body.name,library)
    except (DraftConflict,GenerationError,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@app.post("/api/generation/publish")
def generation_publish(body: GenerationPublishBody,request: Request):
    _generation_origin(request)
    try:
        plex,library=_generation_context()
        draft=get_generation_draft(body.draft_id)
        source=draft["data"].get("source_revisions") or {}
        if source.get("audio") and source["audio"]!=feature_revision(cached_features()[0]):
            raise DraftConflict("AudioMuse evidence changed. Review a regenerated draft.")
        return publish_generation_draft(plex,body.draft_id,body.revision,body.name,
                            body.plan_id,body.approved,library)
    except (DraftConflict,GenerationError,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503,detail=str(exc)) from exc


@app.get("/api/curation/library/status")
def curation_catalogue_status():
    return curation_library_metadata()


@app.get("/api/curation/playlists")
def curation_playlist_names():
    names = [name for name in _configured_playlists()
             if name not in DEFAULT_HIDDEN_PLAYLIST_TITLES]
    return {"playlists": names}


@app.get("/api/curation/playlist/{name:path}")
def curation_playlist(name: str):
    try:
        _, _, library, definitions, live, before = _curation_context(name)
        # Show the playlist ACTUALLY present in Plex; YAML is a separate versioned
        # definition and may be out of sync. Applying updates the matching YAML.
        live_spec = {**definitions[name], "items": [
            {"rating_key": key} for key in before
        ]}
        cached, cache_state = cached_features()
        local_cache = {key: cached[key] for key in before if key in cached}
        from .playlist_curator import features_for
        intent = playlist_identity(name, str(definitions[name].get("description") or ""),
                                   before, library, features_for(local_cache))
        result = assess(name, live_spec, library, local_cache, intent["values"])
        result["sources"]["audiomuse"] = len(cached)
        result["identity"] = intent
        yaml_keys, yaml_issues = resolve_items(definitions[name],library)
        result.update({
            "version": fingerprint(definitions),
            "snapshot": signature(before),
            "playlist_id": str(live.ratingKey),
            "yaml_in_sync": not yaml_issues and yaml_keys == before,
            "yaml_issues": yaml_issues,
            "cache":cache_state,
        })
        return result
    except CurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Cannot inspect the Plex playlist.") from exc


@app.get("/api/curation/identity/{name:path}")
def get_curation_identity(name: str):
    try:
        _, _, library, definitions, _, before = _curation_context(name)
        cached, _ = cached_features()
        from .playlist_curator import features_for
        return playlist_identity(name, str(definitions[name].get("description") or ""),
                                 before, library, features_for({
                                     key: cached[key] for key in before if key in cached
                                 }))
    except IdentityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/curation/identity/{name:path}")
def update_curation_identity(name: str, body: IdentityUpdateBody, request: Request):
    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Same-origin browser request required.")
    if name not in _configured_playlists() or name in DEFAULT_HIDDEN_PLAYLIST_TITLES:
        raise HTTPException(status_code=404, detail="Unknown playlist.")
    try:
        values = put_override(name, body.values, body.revision)
        return {"values":values, "revision":identity_revision(values),"source":"manual"}
    except IdentityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/curation/identity/{name:path}/reset")
def reset_curation_identity(name: str, body: IdentityResetBody, request: Request):
    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Same-origin browser request required.")
    if name not in _configured_playlists() or name in DEFAULT_HIDDEN_PLAYLIST_TITLES:
        raise HTTPException(status_code=404, detail="Unknown playlist.")
    try:
        delete_override(name, body.revision)
        return {"source":"inferred","revision":identity_revision(None)}
    except IdentityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/curation/candidates")
def get_curation_candidates(name: str, target: str = "", q: str = "",
                            limit: int = Query(default=20, ge=1, le=30)):
    if len(q) > 120:
        raise HTTPException(status_code=422, detail="Search query is too long.")
    try:
        _, _, library, definitions, _, before = _curation_context(name)
        cache, _ = cached_features()
        from .playlist_curator import features_for
        intent = playlist_identity(name, str(definitions[name].get("description") or ""),
                                   before, library, features_for({
                                       key: cache[key] for key in before if key in cache
                                   }))
        calibration = validation_calibration(name, intent["values"]["variation"],
                                             current_identity_revision=intent["revision"],
                                             current_playlist_snapshot=signature(before))
        recommendations = rank_candidates(
            before, library, cache, intent, target=target or None,
            query=q, limit=limit, weights=calibration["weights"],
            feedback=validation_feedback(name, intent["revision"],signature(before)))
        # Compute the full paired impact for EVERY visible recommendation.
        # Avoid decoding the whole AudioMuse cache separately for each.
        impact_ids = set(before) | {row["id"] for row in recommendations}
        decoded = features_for({key:cache[key] for key in impact_ids if key in cache})
        pairs = {}
        baseline = None
        for row in recommendations:
            after = list(before)
            if target:
                after[after.index(target)] = row["id"]
            elif row["match"] is not None:
                anchor = row.get("suggested_after_id")
                after.insert(after.index(anchor)+1 if anchor is not None else 0, row["id"])
            else:
                after.append(row["id"])
            row["impact"] = compare_curations(before, after, library, decoded,
                   intent["values"], decoded=True,pair_cache=pairs,baseline=baseline)
            if baseline is None:
                baseline={k:v["before"] for k,v in row["impact"]["metrics"].items()}
        return {"candidates": recommendations,
                "target":target or None, "identity_source":intent["source"],
                "identity_revision":intent["revision"],"calibration":calibration}
    except CurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Candidate search unavailable.") from exc


@app.post("/api/curation/impact")
def curation_impact(body: CurationPlanBody):
    """Compare the exact staged sequence against the live playlist, read-only."""
    try:
        _, _, library, definitions, _, before = _curation_context(body.name)
        if fingerprint(definitions) != body.version or signature(before) != body.snapshot:
            raise CurationConflict("Playlist changed. Reload before comparison.")
        if len(body.operations) > 100:
            raise CurationConflict("Too many staged changes.")
        after = compute_operations(before, body.operations, library)
        cache, _ = cached_features()
        from .playlist_curator import features_for
        intent = playlist_identity(body.name, str(definitions[body.name].get("description") or ""),
                                   before, library, features_for({
                                       key:cache[key] for key in before if key in cache
                                   }))
        if body.identity_revision is not None and body.identity_revision!=intent["revision"]:
            raise CurationConflict("Musical identity changed. Reload before comparison.")
        ids=set(before)|set(after)
        decoded=features_for({key:cache[key] for key in ids if key in cache})
        result=compare_curations(before,after,library,decoded,intent["values"],decoded=True)
        result["identity_revision"]=intent["revision"]
        result["snapshot"]=body.snapshot
        return result
    except (CurationConflict,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@app.post("/api/curation/plan")
def curation_plan(body: CurationPlanBody):
    try:
        _, _, library, definitions, _, before = _curation_context(body.name)
        if fingerprint(definitions) != body.version or signature(before) != body.snapshot:
            raise CurationConflict("Playlist changed. Reload before reviewing.")
        plan = change_plan(before, body.operations, library, name=body.name,
                           version=body.version, snapshot=body.snapshot)
        report = curation_impact(body)
        plan["impact"] = report
        return plan
    except CurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/curation/apply")
def curation_apply(body: CurationApplyBody, request: Request):
    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Same-origin browser request required.")
    try:
        plex, _, library, definitions, _, before = _curation_context(body.name)
        if body.identity_revision is not None:
            cache,_=cached_features()
            from .playlist_curator import features_for
            current = playlist_identity(body.name,str(definitions[body.name].get("description") or ""),
                    before,library,features_for({k:cache[k] for k in before if k in cache}))
            if current["revision"]!=body.identity_revision:
                raise CurationConflict("Musical identity changed after preview. Review the plan again.")
        return apply_approved(
            plex, body.name, version=body.version, snapshot=body.snapshot,
            operations=body.operations, plan_id=body.plan_id,
            library=library, approved=body.approved,
            forbidden=DEFAULT_HIDDEN_PLAYLIST_TITLES,
        )
    except CurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/curation/validation")
def get_curation_validation():
    """Read-only real Plex playlist coverage and cross-genre review queue."""
    definitions=_configured_playlists()
    valid={name:spec for name,spec in definitions.items()
           if name not in DEFAULT_HIDDEN_PLAYLIST_TITLES}
    descriptions={name:str(spec.get("description") or "") for name,spec in valid.items()}
    styles={}
    for name in valid:
        setting=get_override(name)
        if setting: styles[name]=setting["styles"]
    samples=validation_portfolio(valid.keys(),descriptions,styles,per_category=2)
    stats=validation_summary(valid)
    try:
        plex,music=connect()
        library=get_library(music)
        features,_=cached_features()
        live={p.title:p for p in plex.playlists(playlistType="audio")
              if not getattr(p,"smart",False)}
    except Exception as exc:
        raise HTTPException(status_code=503,detail="Could not read Plex for validation.") from exc
    from .playlist_curator import features_for
    rows=[]
    for kind,names in samples.items():
      for name in names:
        playlist=live.get(name)
        if not playlist:
            rows.append({"category":kind,"name":name,"status":"missing_in_plex",
                         **stats[name]})
            continue
        ids=[str(t.ratingKey) for t in current_items(playlist)]
        if not ids or any(k not in library for k in ids):
            rows.append({"category":kind,"name":name,"status":"unresolved_in_catalogue",
                         **stats[name]})
            continue
        local={k:features[k] for k in ids if k in features}
        intent=playlist_identity(name,descriptions[name],ids,library,features_for(local))
        assessment=assess(name,{"items":[{"rating_key":k} for k in ids]},
                          library,local,intent["values"])
        coverage=assessment["profile"]["coverage"]
        current_stats=validation_summary(
            [name],{name:(signature(ids),intent["revision"])})[name]
        reviewed=current_stats["reviewed"]
        distinct=current_stats["distinct_targets"]
        ready=coverage>=.60 and reviewed>=8 and distinct>=4
        rows.append({
            "category":kind,"name":name,
            "status":"validated" if ready else "needs_human_review",
            "tracks":len(ids),"audio_coverage":coverage,
            "identity_source":intent["source"],
            "confidence":assessment["profile"]["confidence"],
            "possible_outliers":len(assessment["reviews"]),
            **current_stats,
        })
    total=sum(row.get("reviewed",0) for row in rows)
    ready=(all(len(samples[cat])>=2 for cat in samples) and
           len(rows)>=8 and all(r["status"]=="validated" for r in rows))
    return {"categories":rows,"reviewed_total":total,
            "audited_groups":len({r["category"] for r in rows}),
            "validation_target":{"playlists_per_category":2,
                                 "listened_reviews_per_playlist":8,
                                 "distinct_targets_per_playlist":4,
                                 "minimum_audio_coverage":.60},
            "human_validation_complete":ready,
            "notice":"Metadata checks and model rankings are not listening judgments; human reviews on actual audio are required."}


@app.get("/api/curation/validation/queue")
def get_curation_validation_queue(name: str):
    """Deterministic diverse listening targets from this *actual* Plex playlist.

    Selecting a target is read-only. Search and listening happen on demand,
    rather than running thousands of recommendation comparisons during audit.
    """
    try:
        _, _, library, definitions, _, ids = _curation_context(name)
        cached,_=cached_features()
        from .playlist_curator import features_for
        local={key:cached[key] for key in ids if key in cached}
        intent=playlist_identity(name,str(definitions[name].get("description") or ""),
                                ids,library,features_for(local))
        assessed=assess(name,{"items":[{"rating_key":key} for key in ids]},
                        library,local,intent["values"])
        unusual=[item["id"] for item in assessed["tracks"]
                 if item["review_flags"]]
        outliers=[item["track_id"] for item in assessed["reviews"][:3]]
        priority=list(dict.fromkeys(unusual[:2]+outliers))[:4]
        spread=[ids[round(i*(len(ids)-1)/7)] for i in range(8)]
        chosen=list(dict.fromkeys(priority+spread))
        # Keep a mix of suspected errors and ordinary examples; only 8
        # distinct targets are needed for a repeatable review session.
        selected=chosen[:8]
        past=[r for r in validation_reviews(name)
              if r["playlist_snapshot"]==signature(ids) and
                 r["identity_revision"]==intent["revision"]]
        targets=[]
        for key in selected:
            track=library[key]
            reviewed=sum(item["target_id"]==key for item in past)
            targets.append({"id":key, "artist":track["artist"],"title":track["title"],
                            "reviewed":reviewed,
                            "reason":"metadata review" if key in unusual else
                                     "contextual outlier" if key in outliers else
                                     "stratified playlist sample"})
        return {"playlist":name,"targets":targets,
                "audio_coverage":assessed["profile"]["coverage"],
                "reviewed":len(past),"distinct_targets":len(
                    {item["target_id"] for item in past}),
                "target_review_count":8,"target_distinct":4,
                "identity_revision":intent["revision"],
                "note":"Sample positions and candidate choices are deterministic. Listening judgments must be made by a person."}
    except CurationConflict as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@app.get("/api/curation/validation/{name:path}")
def get_playlist_validation(name: str):
    if name not in _configured_playlists() or name in DEFAULT_HIDDEN_PLAYLIST_TITLES:
        raise HTTPException(status_code=404,detail="Unknown playlist.")
    definitions=_configured_playlists()
    _, _, library, definitions, _, ids=_curation_context(name)
    cache,_=cached_features()
    from .playlist_curator import features_for
    intent=playlist_identity(name,str(definitions[name].get("description") or ""),
          ids,library,features_for({k:cache[k] for k in ids if k in cache}))
    current=(signature(ids),intent["revision"])
    val=validation_reviews(name)
    for row in val:
        row["current"]=row["playlist_snapshot"]==current[0] and row["identity_revision"]==current[1]
    return {"playlist":name,"reviews":val,
            "summary":validation_summary([name],{name:current})[name],
            "identity_source":intent["source"]}


@app.post("/api/curation/validation/review")
def submit_playlist_validation(body: ValidationBody,request: Request):
    origin=request.headers.get("origin")
    if not origin or origin.rstrip("/")!=str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403,detail="Same-origin browser request required.")
    try:
        import math
        if (not body.listened
            or not math.isfinite(body.candidate_played_seconds)
            or not math.isfinite(body.original_played_seconds)
            or body.candidate_played_seconds < 15
            or (body.target_id and body.original_played_seconds < 15)):
            raise HTTPException(
                status_code=422,
                detail="Audition at least 15 seconds of the candidate and original (if replacing)."
            )
        _, _, library, definitions, _, before=_curation_context(body.name)
        if signature(before)!=body.snapshot:
            raise CurationConflict("Plex playlist changed since listening. Refresh.")
        if body.candidate_id not in library or body.candidate_id in before:
            raise CurationConflict("Candidate is absent from the catalogue or already in playlist.")
        if body.target_id and body.target_id not in before:
            raise CurationConflict("Original track no longer belongs to this playlist.")
        cache,_=cached_features()
        from .playlist_curator import features_for
        intent=playlist_identity(body.name,str(definitions[body.name].get("description") or ""),
              before,library,features_for({k:cache[k] for k in before if k in cache}))
        if intent["revision"]!=body.identity_revision:
            raise CurationConflict("Identity changed since audition. Reload.")
        # Recompute factual candidate factors server-side. Do not trust
        # fabricated scores or feature snapshots supplied by a browser.
        # An individually selected candidate need not be among the top N.
        # Evaluate it through the same scoring function directly.
        from .playlist_ranking import _profile_context,_score_candidate
        ids=set(before)|{body.candidate_id}
        ff=features_for({k:cache[k] for k in ids if k in cache})
        ctx=_profile_context(before,library,ff,intent["values"],body.target_id)
        scored=_score_candidate(body.candidate_id,library[body.candidate_id],
                  ff.get(body.candidate_id,{}),before,library,ff,intent["values"],
                  body.target_id,ctx,detailed=True)
        saved=record_validation(body.name,body.target_id,body.candidate_id,
                   body.verdict,body.issue,body.note,body.listened,
                   intent["revision"],body.snapshot,scored["match"],
                   scored.get("factors",{}))
        saved["calibration"]=validation_calibration(body.name,intent["values"]["variation"],
                            current_identity_revision=intent["revision"],
                            current_playlist_snapshot=body.snapshot)
        return saved
    except (CurationConflict,ValueError) as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc


@app.get("/api/curation/audio/{track_id}")
def curation_audio(track_id: int, request: Request):
    """Proxy selected Plex audio bytes; never expose the Plex token to the browser."""
    import requests
    from .plex_client import discover_token
    if track_id <= 0:
        raise HTTPException(status_code=404, detail="Invalid track.")
    try:
        plex, _ = connect()
        song = plex.fetchItem(track_id)
        if getattr(song,"TYPE",getattr(song,"type","")) != "track":
            raise HTTPException(status_code=404, detail="Not an audio track.")
        part = song.media[0].parts[0]
        path = str(part.key)
        if not path.startswith("/library/parts/"):
            raise HTTPException(status_code=404, detail="Audio file unavailable.")
        range_header = request.headers.get("range", "")
        if range_header and (len(range_header) > 80 or not range_header.startswith("bytes=")
                             or "," in range_header):
            raise HTTPException(status_code=416, detail="Unsupported audio range.")
        headers = {"X-Plex-Token": discover_token()}
        if range_header:
            headers["Range"] = range_header
        upstream = requests.get(plex._baseurl.rstrip("/") + path, headers=headers,
                                stream=True, timeout=(5, 30), allow_redirects=False,
                                verify=plex._session.verify)
        if upstream.status_code not in (200, 206):
            upstream.close()
            raise HTTPException(status_code=502, detail="Plex could not stream this track.")
        safe = {k: v for k,v in (
            ("Accept-Ranges",upstream.headers.get("Accept-Ranges")),
            ("Content-Range",upstream.headers.get("Content-Range")),
            ("Content-Length",upstream.headers.get("Content-Length")),
        ) if v}
        kind = str(getattr(part,"container","") or "").lower()
        mime = {"mp3":"audio/mpeg","flac":"audio/flac","m4a":"audio/mp4",
                "mp4":"audio/mp4","ogg":"audio/ogg","wav":"audio/wav"}.get(kind,"application/octet-stream")
        def chunks():
            try:
                yield from upstream.iter_content(chunk_size=65536)
            finally:
                upstream.close()
        return StreamingResponse(chunks(), status_code=upstream.status_code,
                                 media_type=mime, headers=safe)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Audio preview unavailable.") from exc
