from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .config_store import load, read_text, save_text
from .plex_client import connect, resolve_playlist, sync_playlist


app = FastAPI(title="Plex Playlist Manager", version="1.0.0")
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
        return {\n            "text": read_text(),\n            "playlists": list(data.get("playlists", {}).keys()),\n            "definitions": data.get("playlists", {}),\n        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/config")
def put_config(body: ConfigBody):
    try:
        data = save_text(body.text)
        return {"ok": True, "playlists": list(data.get("playlists", {}).keys())}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/preview/{name}")
def preview(name: str):
    try:
        playlists = _configured_playlists()
        if name not in playlists:
            raise HTTPException(status_code=404, detail="Unknown playlist")
        plex, music = connect()
        tracks, matches = resolve_playlist(music, playlists[name])
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
