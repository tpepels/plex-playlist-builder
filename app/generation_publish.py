"""Explicitly approved creation of *new* Plex playlists and matching YAML entries.

A single transaction holds the draft/YAML write locks; existing Plex playlists
are never deleted or overwritten. Partial failures attempt rollback and return
a reconciliation error rather than claiming success.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading

import yaml

from .config_store import fingerprint, load, read_text, save_text, mark_synced
from .curation_apply import current_keys
from .playlist_curator import yaml_items
from .generation_store import LOCK as DRAFT_LOCK, get, mark_published, DraftConflict
from .playlist_generation import diagnose, GenerationError

PUBLISH_LOCK=threading.RLock()


def _name(value):
    if not isinstance(value,str):raise GenerationError("Playlist name must be text.")
    title=value.strip()
    if not 2<=len(title)<=120 or any(ord(c)<32 for c in title):
        raise GenerationError("Choose a playlist name between 2 and 120 characters.")
    return title


def _lookup(plex,name):
    return [p for p in plex.playlists(playlistType="audio")
            if p.title.casefold()==name.casefold()]


def _validate(data,library):
    keys=data.get("keys")
    if not isinstance(keys,list) or not keys or len(keys)>120 or len(set(keys))!=len(keys):
        raise DraftConflict("Draft must contain 1–120 different tracks.")
    if any(k not in library for k in keys):
        raise DraftConflict("A draft track is absent from the current Plex catalogue.")
    return keys


def _fingerprint(ident,revision,name,keys,version):
    data={"draft":ident,"revision":revision,"title":name,
          "keys":keys,"yaml_version":version}
    return hashlib.sha256(json.dumps(data,sort_keys=True,
                         separators=(",",":")).encode()).hexdigest()[:32]


def prepare(plex,draft,title,library):
    title=_name(title)
    if draft["status"]!="draft":
        raise DraftConflict("This draft has already been published.")
    keys=_validate(draft["data"],library)
    current=load()
    defs=current.get("playlists",{})
    if any(name.casefold()==title.casefold() for name in defs) or _lookup(plex,title):
        raise DraftConflict("A playlist with this name already exists in Plex or YAML.")
    version=fingerprint(defs)
    token=_fingerprint(draft["id"],draft["revision"],title,keys,version)
    return {"draft_id":draft["id"],"revision":draft["revision"],
            "name":title,"tracks":len(keys),
            "minutes":round(sum(int(library[k].get("duration_ms") or 0) for k in keys)/60000,1),
            "yaml_version":version,"plan_id":token,
            "quality":draft["data"]["diagnostics"],
            "warnings":draft["data"]["warnings"],
            "track_order":[{"id":k,"artist":library[k]["artist"],"album":library[k]["album"],
                            "title":library[k]["title"]} for k in keys]}


def publish(plex,draft_id,revision,title,plan_id,approved,library):
    if approved is not True:raise DraftConflict("Explicit publication approval required.")
    with PUBLISH_LOCK,DRAFT_LOCK:
        draft=get(draft_id)
        if revision!=draft["revision"]:raise DraftConflict("Draft changed. Review it again.")
        plan=prepare(plex,draft,title,library)
        if plan_id!=plan["plan_id"]:raise DraftConflict("Publication plan changed. Review it again.")
        keys=draft["data"]["keys"]
        # Fetch and type-check every Plex media object BEFORE creating anything.
        tracks=[]
        for key in keys:
            item=plex.fetchItem(int(key))
            if str(getattr(item,"TYPE",getattr(item,"type",""))).lower()!="track":
                raise DraftConflict("Plex returned a non-music item for track "+key)
            tracks.append(item)
        original=read_text()
        previous=load()
        updated=dict(previous)
        defs=dict(previous.get("playlists",{}))
        entry={"description":draft["data"]["request"]["prompt"],
               "enabled":True,"strict":True,
               "generation":{"source":"Intelligent Playlist Generation",
                   "criteria":draft["data"]["request"]["criteria"]},
               "items":yaml_items(keys,library)}
        defs[title]=entry
        updated["playlists"]=defs
        created=None
        saved=False
        creation_attempted=False
        try:
            creation_attempted=True
            created=plex.createPlaylist(title=title,items=tracks)
            if not getattr(created,"ratingKey",None):
                raise RuntimeError("Plex returned no playlist identity.")
            if current_keys(created)!=keys:
                raise RuntimeError("New playlist order differs from the approved plan.")
            save_text(yaml.safe_dump(updated,sort_keys=False,allow_unicode=True))
            saved=True
            result=mark_published(draft_id,revision,created.ratingKey,title)
        except Exception as error:
            failures=[]
            if saved or (fingerprint(load().get("playlists",{}))!=
                         fingerprint(previous.get("playlists",{}))):
                try:save_text(original)
                except Exception as rollback_error:
                    failures.append("YAML restore: "+str(rollback_error))
            if created is not None and getattr(created,"ratingKey",None):
                try:created.delete()
                except Exception as rollback_error:
                    failures.append("Plex removal: "+str(rollback_error))
            if created is None and creation_attempted:
                failures.append("Plex creation outcome unknown (no returned playlist identity)")
            if created is not None and not getattr(created,"ratingKey",None):
                failures.append("Plex returned no playlist identity; creation outcome unknown")
            if failures:
                raise RuntimeError("Publication failed. Manual reconciliation required: "+
                                   "; ".join(failures)+"; cause: "+str(error)) from error
            raise RuntimeError("Publication failed; newly created Plex playlist and YAML were rolled back.") from error
        try:
            mark_synced(title,entry)
            sync_state="synced"
        except Exception:
            sync_state="state_repair_required"
        return {"status":"published","sync_state":sync_state,
                "draft_id":draft_id,"name":title,
                "playlist_id":str(created.ratingKey),"tracks":len(keys),
                "verified":True,"draft_status":result["status"]}
