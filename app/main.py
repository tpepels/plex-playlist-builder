from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .config_store import load, read_text, save_text
from .plex_client import connect, resolve_playlist, sync_playlist


app = FastAPI(title="Plex Playlist Manager", version="1.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class ConfigBody(BaseModel):
    text: str


def _configured_playlists():
    return load().get("playlists", {})


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
        data = load()
        return {
            "text": read_text(),
            "playlists": list(data.get("playlists", {}).keys()),
            "definitions": data.get("playlists", {}),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/config")
def put_config(body: ConfigBody):
    try:
        data = save_text(body.text)
        return {"ok": True, "playlists": list(data.get("playlists", {}).keys())}
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
        plex, music = connect()
        return sync_playlist(plex, music, name, playlists[name])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/sync-all")
def sync_all():
    try:
        playlists = _configured_playlists()
        plex, music = connect()
        results = []
        for name, spec in playlists.items():
            if spec.get("enabled", True):
                results.append(sync_playlist(plex, music, name, spec))
        return {"results": results}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
