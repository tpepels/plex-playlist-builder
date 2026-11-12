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


def _plex_metadata(track):
    """Capture available Plex Track XML attributes and structured music tags.

    Preserve unknown future Plex metadata without serializing PlexAPI's
    connection/session objects. Filesystem paths and bearer tokens are
    excluded; media codec/bitrate attributes remain available.
    """
    element=getattr(track,"_data",None)
    if element is None or not hasattr(element,"attrib"):
        return {}
    hidden={"file","token","x-plex-token","X-Plex-Token","authToken"}
    def safe(attrs):
        return {str(k):str(v) for k,v in attrs.items()
                if str(k).casefold() not in hidden}
    data={"track_attributes":safe(element.attrib),"tags":{},"media":[]}
    for child in element:
        tag=str(child.tag)
        if tag=="Media":
            media={"attributes":safe(child.attrib)}
            streams=[]
            for part in child:
                if part.tag=="Part":
                    media.setdefault("parts",[]).append(safe(part.attrib))
                    for stream in part:
                        if stream.tag=="Stream":
                            streams.append(safe(stream.attrib))
            if streams:
                media["streams"]=streams
            data["media"].append(media)
        else:
            data["tags"].setdefault(tag,[]).append(safe(child.attrib))
    return data


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
       "year":row.get("year"),
       "plex_metadata":row.get("plex_metadata") or {}} for k,row in tracks.items()}
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
    try:
        stat = FILE.stat()
        signature = (str(FILE), stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        signature = (str(FILE), None, None)
    with _LOCK:
        cached = _CACHED.get("snapshot")
        if cached is None or _CACHED.get("signature") != signature:
            cached = _read()
            _CACHED.clear()
            if cached:
                _CACHED.update(snapshot=cached, signature=signature)
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
        for source in all_tracks:
            key=str(getattr(source,"ratingKey","") or "")
            if key in tracks:
                tracks[key]["plex_metadata"]=_plex_metadata(source)
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
        stat = FILE.stat()
        with _LOCK:
            _CACHED.update(snapshot=new,
                           signature=(str(FILE), stat.st_mtime_ns, stat.st_size))
        return new["tracks"]

def metadata():
    with _LOCK:
        data=_CACHED.get("snapshot") or _read()
        return {"tracks":len(data["tracks"]),"updated":data["updated"]} if data else {"tracks":0,"updated":None}
