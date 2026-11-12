"""Read-only comparison of the saved playlist library with actual Plex objects.

State is persisted independently of YAML and Plex so GET endpoints do not
run expensive Plex scans. Never silently overwrite Plex changes made elsewhere.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import tempfile

from .config_store import load, fingerprint
from .plex_client import connect, resolve_playlist, _is_hidden_audio_playlist

FILE=Path(os.getenv("PLAYLIST_DRIFT_FILE","/data/playlist-drift.json"))
INTERVAL_SECONDS=5*60
_LAST_ERROR=""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(report):
    FILE.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=".playlist-drift-",dir=FILE.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"w",encoding="utf-8") as output:
            json.dump(report,output,ensure_ascii=False,separators=(",",":"))
            output.flush();os.fsync(output.fileno())
        os.replace(name,FILE)
    finally:
        Path(name).unlink(missing_ok=True)


def snapshot():
    try:
        report=json.loads(FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok":not bool(_LAST_ERROR),"checked_at":None,"playlists":{},
                "counts":{},"status":"pending" if not _LAST_ERROR else "error",
                "error":_LAST_ERROR or None}
    except (OSError,ValueError) as exc:
        return {"ok":False,"checked_at":None,"playlists":{},
                "counts":{},"error":str(exc),"status":"error"}
    if not isinstance(report,dict) or not isinstance(report.get("playlists"),dict):
        return {"ok":False,"checked_at":None,"playlists":{},
                "counts":{},"error":"Invalid live playlist snapshot","status":"error"}
    if _LAST_ERROR:
        return {**report,"ok":False,"error":_LAST_ERROR}
    return report


def refresh():
    global _LAST_ERROR
    definitions=load().get("playlists",{})
    plex,music=connect()
    live=list(plex.playlists(playlistType="audio"))
    by_name={}
    for p in live:
        by_name.setdefault(str(p.title).casefold(),[]).append(p)
    results={}
    for name,spec in definitions.items():
        found=by_name.get(name.casefold(),[])
        entry={"state":"pending","reason":"","playlist_id":None,
               "expected_count":None,"actual_count":None,"added":0,"missing":0,
               "reordered":False}
        if len(found)>1:
            entry.update(state="ambiguous",reason="Multiple Plex playlists match this name")
        elif not found:
            entry.update(state="missing",reason="Saved playlist has no live Plex object")
        else:
            playlist=found[0]
            entry["playlist_id"]=str(getattr(playlist,"ratingKey","") or "")
            if _is_hidden_audio_playlist(playlist):
                entry.update(state="protected",reason="Plex smart/radio playlist is not editable")
            else:
                from .curation_apply import current_keys
                actual=current_keys(playlist)
                entry["actual_count"]=len(actual)
                try:
                    expected,matches=resolve_playlist(plex,music,spec)
                except Exception as exc:
                    entry.update(state="unverifiable",
                                 reason="Cannot resolve saved items: "+str(exc)[:160])
                else:
                    target=[str(t.ratingKey) for t in expected]
                    entry["expected_count"]=len(target)
                    if any(m.status!="matched" for m in matches):
                        entry.update(state="unverifiable",
                                     reason="Some YAML items cannot be resolved to Plex tracks")
                    elif target==actual:
                        entry.update(state="in_sync",
                                     reason="Live track order matches saved playlist")
                    else:
                        before=set(target);after=set(actual)
                        entry.update(state="drift",
                                     reason="Live Plex track membership or order differs from saved YAML",
                                     added=len(after-before),missing=len(before-after),
                                     reordered=bool(before==after and target!=actual))
        results[name]=entry
    report={"ok":True,"status":"checked","checked_at":_now(),
            "yaml_revision":fingerprint(definitions),
            "playlists":results,
            "counts":dict(Counter(e["state"] for e in results.values()))}
    _write(report)
    _LAST_ERROR=""
    return report


async def loop():
    delay=25
    while True:
        await asyncio.sleep(delay)
        delay=INTERVAL_SECONDS
        try:
            await asyncio.to_thread(refresh)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            global _LAST_ERROR
            # Preserve the last successful snapshot but show the stale/error
            # state, rather than silently treating an old comparison as fresh.
            _LAST_ERROR=str(exc)[:240]
