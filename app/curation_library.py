"""Persistent complete Plex music track catalogue for interactive curation.

A bounded PlexAPI paginated scan updates a single atomic JSON snapshot; reads
between scans use the previous complete library, never a partial page.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

from .playlist_curator import catalogue

FILE = Path(os.getenv("CURATION_LIBRARY_FILE","/data/curation-library.json"))
MAX_AGE_SECONDS = 3600
_LOCK = threading.RLock()
_CACHED = {}


def _valid(value):
    return isinstance(value,dict) and bool(value) and all(
        isinstance(key,str) and isinstance(row,dict)
        and all(field in row for field in ("key","artist","album","title","album_key","disc","index"))
        for key,row in value.items()
    )


def _read():
    try:
        data=json.loads(FILE.read_text(encoding="utf-8"))
        if (isinstance(data,dict) and isinstance(data.get("updated"),(int,float))
                and _valid(data.get("tracks"))):
            return data
    except (OSError,ValueError,TypeError):
        pass
    return None


def _write(tracks):
    FILE.parent.mkdir(parents=True,exist_ok=True)
    # Only JSON-native fields; the full searchTracks() objects are not persisted.
    cleaned={k:{**{field:row[field] for field in (
        "key","album_key","artist","album","title","disc","index"
    )},"genres":list(row.get("genres") or [])[:16],
       "duration_ms":row.get("duration_ms")} for k,row in tracks.items()}
    envelope={"updated":time.time(),"tracks":cleaned}
    temp=FILE.with_suffix(FILE.suffix+".tmp")
    temp.write_text(json.dumps(envelope,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    os.replace(temp,FILE)
    return envelope


def get_library(music, refresh=False):
    with _LOCK:
        cached=_CACHED.get("snapshot")
        if cached is None:
            cached=_read()
            if cached: _CACHED["snapshot"]=cached
        if not refresh and cached and time.time()-cached["updated"] < MAX_AGE_SECONDS:
            return cached["tracks"]
        # PlexAPI pagination is explicit; the returned value is complete only
        # when all search pages succeeded, otherwise retain the prior snapshot.
        all_tracks=music.searchTracks(container_size=200)
        tracks=catalogue(all_tracks)
        if not _valid(tracks):
            raise RuntimeError("Plex returned an empty or invalid track catalogue.")
        new=_write(tracks)
        _CACHED["snapshot"]=new
        return new["tracks"]


def metadata():
    with _LOCK:
        data=_CACHED.get("snapshot") or _read()
        return {"tracks":len(data["tracks"]),"updated":data["updated"]} if data else {"tracks":0,"updated":None}
