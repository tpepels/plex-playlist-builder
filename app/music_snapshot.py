"""Bounded shared disk/Plex snapshot for read-only album diagnostics.

Filtering changes must not rescan the entire music volume or Plex library.
Real publications or explicit rescan requests invalidate the snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

TTL_SECONDS=120
_LOCK=threading.Lock()
_CACHE={}


def invalidate():
    with _LOCK:
        _CACHE.clear()


def get(inventory_provider, plex_connector, *, force=False):
    key=(id(inventory_provider),id(plex_connector))
    with _LOCK:
        if (not force and _CACHE.get("key")==key and
            time.monotonic()-_CACHE.get("at",0)<TTL_SECONDS):
            return {**_CACHE["data"],"cached":True}
        # Serialize expensive scans so rapid keystrokes cannot initiate
        # overlapping disk and Plex traversals.
        inventory=inventory_provider()
        _,music=plex_connector()
        tracks=music.searchTracks()
        data={
            "inventory":inventory,
            "tracks":tracks,
            "locations":[str(x) for x in
                         (getattr(music,"locations",None) or []) if x],
            "fetched_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _CACHE.update(key=key,at=time.monotonic(),data=data)
        return {**data,"cached":False}
