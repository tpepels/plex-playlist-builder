from __future__ import annotations

import csv
import io
from pathlib import Path

import yaml

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .config_store import (
    load,
    mark_synced,
    merge_text,
    parse_text,
    pending_names,
    read_text,
    save_text,
)
from .plex_client import (
    DEFAULT_HIDDEN_PLAYLIST_TITLES,
    connect,
    export_audio_playlists,
    hidden_audio_playlist_titles,
    resolve_playlist,
    sync_playlist,
)


app = FastAPI(title="Plex Playlist Manager", version="1.3.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class ConfigBody(BaseModel):
    text: str


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
    pending = [
        name
        for name in pending_names(definitions.keys())
        if name not in hidden
    ]

    ordered: dict = {}
    for name in pending:
        ordered[name] = definitions[name]
    for name, spec in definitions.items():
        if name not in hidden and name not in ordered:
            ordered[name] = spec

    return {
        "playlists": list(ordered.keys()),
        "definitions": ordered,
        "pending": pending,
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


@app.post("/api/import-config")
def import_config(body: ConfigBody):
    try:
        current = _configured_playlists()
        incoming = parse_text(body.text).get("playlists", {})
        changed = [
            name
            for name, spec in incoming.items()
            if current.get(name) != spec
        ]
        data = merge_text(body.text)
        payload = _config_payload(data)
        visible = set(payload["playlists"])
        return {
            "ok": True,
            **payload,
            "changed": [name for name in changed if name in visible],
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
            mark_synced(name)
        result["pending"] = name in pending_names(playlists.keys())
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
                mark_synced(name)
            results.append(result)
        return {"results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
