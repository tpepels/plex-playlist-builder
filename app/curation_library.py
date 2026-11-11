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
_LOCK = threading.RLock()  # protects the in-memory snapshot only
_REFRESH_LOCK = threading.Lock()  # only one full Plex scan may publish
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
       "duration_ms":row.get("duration_ms"),
       "year":row.get("year")} for k,row in tracks.items()}
    envelope={"updated":time.time(),"tracks":cleaned}
    temp=FILE.with_suffix(FILE.suffix+".tmp")
    temp.write_text(json.dumps(envelope,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    os.replace(temp,FILE)
    return envelope


def cached_library():
    """Return the last complete local Plex catalogue without a network request.

    Explicitly separate read-only UI lookups from scheduled/network refreshes.
    No snapshot exists until the first complete scan succeeds.
    """
    with _LOCK:
        cached = _CACHED.get("snapshot")
        if cached is None or _CACHED.get("path") != str(FILE):
            cached = _read()
            _CACHED.clear()
            if cached:
                _CACHED.update(snapshot=cached, path=str(FILE))
        return cached["tracks"] if cached else None


def get_library(music, refresh=False):
    # Normal UI reads use the most recent complete snapshot immediately. A
    # scheduled background refresh is responsible for freshness and must never
    # block a browser request behind a long network-wide Plex scan.
    if not refresh:
        cached = cached_library()
        if cached is not None:
            return cached
    with _REFRESH_LOCK:
        # A second ordinary reader may have scanned while we waited.
        if not refresh:
            cached = cached_library()
            if cached is not None:
                return cached
        # PlexAPI pagination is explicit; the returned value is complete only
        # when all search pages succeeded, otherwise retain the prior snapshot.
        all_tracks=music.searchTracks(container_size=200)
        tracks=catalogue(all_tracks)
        # Plex track objects often omit the genre/year stored on their album.
        # Enrich by parentRatingKey using one bulk album search (not one
        # network request per track). A failed album search leaves unknowns
        # explicit, and never invents release metadata.
        try:
            albums=music.searchAlbums(container_size=200)
        except (AttributeError,TypeError):
            albums=[]
        except Exception:
            albums=[]
        album_data={}
        for album in albums:
            key=str(getattr(album,"ratingKey","") or "")
            if not key:continue
            album_data[key]={
                "year":getattr(album,"year",None),
                "genres":[str(getattr(tag,"tag",tag) or "") for tag in
                           (getattr(album,"genres",None) or [])][:16],
            }
        for row in tracks.values():
            inherited=album_data.get(row["album_key"],{})
            if not row.get("year") and inherited.get("year"):
                row["year"]=inherited["year"]
            if not row.get("genres") and inherited.get("genres"):
                row["genres"]=inherited["genres"]
        if not _valid(tracks):
            raise RuntimeError("Plex returned an empty or invalid track catalogue.")
        new=_write(tracks)
        with _LOCK:
            _CACHED.update(snapshot=new, path=str(FILE))
        return new["tracks"]

def metadata():
    with _LOCK:
        data=_CACHED.get("snapshot") or _read()
        return {"tracks":len(data["tracks"]),"updated":data["updated"]} if data else {"tracks":0,"updated":None}
