"""Precondition-checked, in-place playlist curation.

Only explicit client-approved actions can write to Plex. Keeps the original
playlist object, cover and rating key. Any partial Plex failure attempts an
in-place rollback; it is never silently reported as success.
"""
from __future__ import annotations

import hashlib
import json
import threading

from .config_store import fingerprint, load, mark_synced, save_text
from .playlist_curator import CurationConflict, compute_operations, signature, yaml_items
import yaml

_WRITE_LOCK = threading.Lock()


def find_live(plex, name: str):
    found = [p for p in plex.playlists(playlistType="audio") if p.title == name]
    if len(found) != 1:
        raise CurationConflict("Expected exactly one existing Plex audio playlist.")
    p = found[0]
    if getattr(p,"smart",False) or getattr(p,"radio",False):
        raise CurationConflict("Cannot edit a smart or radio playlist.")
    return p


def current_items(playlist):
    # PlexAPI's playlist.items() caches results: invalidate after each write.
    playlist.__dict__.pop("_items", None)
    return list(playlist.items())


def current_keys(playlist):
    return [str(t.ratingKey) for t in current_items(playlist)]


def change_plan(before: list[str], operations: list[dict], library: dict,
                *, name: str, version: str, snapshot: str) -> dict:
    after = compute_operations(before, operations, library)
    plan = {"name":name,"version":version,"snapshot":snapshot,"after":after}
    plan_id = hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(",",":")).encode()).hexdigest()[:24]
    before_set, after_set = set(before), set(after)
    return {
        "plan_id":plan_id, "before_count":len(before),"after_count":len(after),
        "added":[library[k] for k in after if k not in before_set],
        "removed":[library[k] for k in before if k not in after_set],
        "new_order":[{"id":k, "artist":library[k]["artist"],"title":library[k]["title"]} for k in after],
        "after":after,
    }


def _apply_in_place(playlist, target: list[str], objects: dict[str, object]):
    existing = current_keys(playlist)
    additions = [key for key in target if key not in existing]
    if additions:
        playlist.addItems([objects[key] for key in additions])
    for item in current_items(playlist):
        if str(item.ratingKey) not in target:
            playlist.removeItems(item)
            current_items(playlist)  # Keep PlexAPI item-ID lookup current.
    for position, key in enumerate(target):
        live = current_items(playlist)
        if position < len(live) and str(live[position].ratingKey) == key:
            continue
        chosen = next((item for item in live if str(item.ratingKey)==key),None)
        if chosen is None:
            raise CurationConflict("Plex lost a track while reordering.")
        previous = live[position-1] if position else None
        playlist.moveItem(chosen, after=previous)
    if current_keys(playlist) != target:
        raise CurationConflict("Plex returned an unexpected playlist ordering.")


def apply_approved(plex, name: str, *, version: str, snapshot: str,
                   operations: list[dict], plan_id: str, library: dict,
                   approved: bool, forbidden: set[str]) -> dict:
    if not approved:
        raise CurationConflict("Explicit approval is required.")
    if name in forbidden:
        raise CurationConflict("System playlists cannot be curated.")
    if not _WRITE_LOCK.acquire(blocking=False):
        raise CurationConflict("Another playlist update is active.")
    try:
        data = load()
        definitions = data.get("playlists", {})
        if name not in definitions:
            raise CurationConflict("Playlist no longer exists in YAML.")
        if fingerprint(definitions) != version:
            raise CurationConflict("YAML changed. Reload before applying.")
        playlist = find_live(plex, name)
        if not getattr(playlist,"ratingKey",None):
            raise CurationConflict("Plex playlist identity is missing.")
        before = current_keys(playlist)
        if len(set(before)) != len(before):
            raise CurationConflict("Plex contains duplicate track IDs. Resolve duplicates first.")
        if signature(before) != snapshot:
            raise CurationConflict("Plex playlist changed. Reload before applying.")
        plan = change_plan(before,operations,library,name=name,version=version,snapshot=snapshot)
        if plan_id != plan["plan_id"]:
            raise CurationConflict("Approval does not match the current plan.")
        after = plan["after"]
        # Fetch all tracks before editing any playlist items.
        missing = [key for key in after if key not in library]
        if missing:
            raise CurationConflict("Missing catalogue IDs: "+", ".join(missing[:5]))
        required = {key for key in after if key not in before}
        objects = {}
        for key in required:
            item = plex.fetchItem(int(key))
            if str(getattr(item,"TYPE",getattr(item,"type",""))) != "track":
                raise CurationConflict("Candidate is not a music track: "+key)
            objects[key] = item
        new_spec = dict(definitions[name])
        new_spec["items"] = yaml_items(after,library)
        changed = dict(data)
        changed_defs = dict(definitions)
        changed_defs[name] = new_spec
        changed["playlists"] = changed_defs
        try:
            _apply_in_place(playlist,after,objects)
            # Persist YAML AFTER verified Plex mutation. On YAML failure,
            # try to restore Plex to the original ordered content.
            save_text(yaml.safe_dump(changed,sort_keys=False,allow_unicode=True))
        except Exception as exc:
            errors = []
            # save_text writes YAML before writing its pending-state file. A
            # failure in that SECOND step must restore YAML as well as Plex.
            try:
                if fingerprint(load()) != fingerprint(data):
                    save_text(yaml.safe_dump(data,sort_keys=False,allow_unicode=True))
            except Exception as yaml_rollback:
                errors.append("YAML rollback: "+str(yaml_rollback))
            try:
                restore = {}
                live_after_failure = current_keys(playlist)
                for key in before:
                    if key not in live_after_failure:
                        restore[key] = plex.fetchItem(int(key))
                _apply_in_place(playlist,before,restore)
            except Exception as plex_rollback:
                errors.append("Plex rollback: "+str(plex_rollback))
            if errors:
                raise RuntimeError(
                    "Update failed; manual reconciliation required: " +
                    "; ".join(errors) + "; original error: "+str(exc)
                ) from exc
            raise RuntimeError("Update failed; Plex and YAML were restored.") from exc
        try:
            mark_synced(name,new_spec)
            state="synced"
        except Exception:
            # The saved YAML is already correct; do not roll back a confirmed write.
            state="saved_needs_state_repair"
        return {
            "status":state, "name":name,"playlist_id":str(playlist.ratingKey),
            "before_count":len(before),"after_count":len(after),
            "added":len(plan["added"]),"removed":len(plan["removed"]),
            "snapshot":signature(after), "verified":True,
        }
    finally:
        _WRITE_LOCK.release()
