"""Round-trip, read-only Plex/AudioMuse export and reviewed playlist proposals.

Export always uses a complete persisted Plex catalogue and the raw AudioMuse
SQLite snapshot. Proposal preview never writes data. Approval uses the existing
in-place curation and generation-publication transactions, not the legacy
delete-and-recreate bulk playlist synchronizer.
"""
from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import struct
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config_store import fingerprint, load
from .curation_apply import (
    _WRITE_LOCK, apply_approved, change_plan, current_keys, find_live,
)
from .generation_publish import (
    PUBLISH_LOCK, _name, prepare as prepare_generation,
    publish as publish_generation,
)
from .generation_store import create as create_draft
from .playlist_curator import CurationConflict, signature
from .playlist_generation import catalogue_revision
from .audiomuse_client import feature_revision
from .plex_client import DEFAULT_HIDDEN_PLAYLIST_TITLES

EXPORT_VERSION = 1
PROPOSAL_FORMAT = "plex-playlist-proposal-v1"
MAX_PROPOSAL_BYTES = 2 * 1024 * 1024
MAX_PROPOSALS = 30
MAX_TITLE = 120
MAX_DESC = 2000
# Preview tokens are process-local: a restart forces a fresh review.
_SECRET = secrets.token_bytes(32)


class CollaborationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> str:
    # Standard JSON cannot encode NaN/Infinity. Retain even those invalid
    # upstream measurements with an explicit, reversible marker.
    def safe(item):
        if isinstance(item,float) and not math.isfinite(item):
            return {"__nonfinite_float__": str(item)}
        if isinstance(item,dict):
            return {str(key):safe(entry) for key,entry in item.items()}
        if isinstance(item,(list,tuple)):
            return [safe(entry) for entry in item]
        return item
    return json.dumps(safe(value), ensure_ascii=False, separators=(",", ":"),
                      default=str, allow_nan=False)


def _vector(value):
    """Decode full, unnormalized little-endian float32 embeddings.

    The base64 payload is exported unchanged even if decoding fails.
    """
    if value is None:
        return None, "missing"
    if not isinstance(value, str) or not value:
        return None, "invalid"
    try:
        data = base64.b64decode(value, validate=True)
        if len(data) < 16 or len(data) > 4 * 4096 or len(data) % 4:
            return None, "invalid"
        numbers = struct.unpack("<" + "f" * (len(data)//4), data)
        if not all(math.isfinite(n) for n in numbers):
            return None, "invalid"
        return list(numbers), "available"
    except (ValueError, binascii.Error, struct.error):
        return None, "invalid"


def _coverage(raw: dict | None):
    if raw is None:
        return "missing_analysis", "missing", "missing"
    music, mstate = _vector(raw.get("embedding"))
    clap, cstate = _vector(raw.get("clap_embedding"))
    if mstate == cstate == "available":
        status = "complete_embeddings"
    elif mstate == "available" or cstate == "available":
        status = "partial_embeddings"
    elif "invalid" in (mstate, cstate):
        status = "invalid_embeddings"
    else:
        status = "no_embeddings"
    return status, mstate, cstate


def _raw_scalars(raw: dict | None):
    """No AudioMuse measurement is silently dropped from the CSV."""
    if raw is None:
        return {}
    return {key: value for key, value in raw.items()
            if key not in ("embedding", "clap_embedding")}


def _moods(raw: dict | None):
    value = (raw or {}).get("other_features") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def build_export_zip(library: dict, features: dict, definitions: dict,
                     *, catalogue_updated=None, cache_state: dict | None = None,
                     live_playlists: dict | None = None):
    """Write a ZIP on disk without retaining duplicate giant embeddings in RAM.

    Returns (temporary_path, manifest); caller owns unlinking the file.
    All cached AudioMuse records are exported, including no-longer-indexed
    track IDs. Every current Plex track gets an explicit missing-data row.
    """
    if not library:
        raise CollaborationError("Plex catalogue is empty. Wait for a complete library index.")
    cache_state = cache_state or {}
    ids = sorted(library, key=lambda x: (x.casefold(), x))
    unknown = sorted(set(features) - set(library))
    counts = {
        "plex_tracks": len(ids),
        "plex_albums": len({row.get("album_key") for row in library.values()}),
        "cached_audio_records": len(features),
        "orphan_audio_records": len(unknown),
        "missing_analysis": 0,
        "no_embeddings": 0,
        "partial_embeddings": 0,
        "complete_embeddings": 0,
        "invalid_embeddings": 0,
        "tracks_without_enriched_plex_metadata": sum(
            "plex_metadata" not in row for row in library.values()
        ),
    }
    fields = [
        "rating_key", "album_rating_key", "artist", "album", "title",
        "disc", "track", "year", "duration_ms", "genres_json",
        "plex_metadata_json", "analysis_status", "musicnn_status", "clap_status",
        "energy", "tempo", "musical_key", "scale",
        "mood_vector_json", "other_features_json",
        "audio_analysis_raw_json",
    ]
    descriptor, filename = tempfile.mkstemp(prefix="music-collaboration-", suffix=".zip")
    os.close(descriptor)
    try:
        with zipfile.ZipFile(filename, mode="w",
                             compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as archive:
            with archive.open("tracks.csv", "w") as binary:
                with io.TextIOWrapper(binary, encoding="utf-8", newline="") as file:
                    writer = csv.DictWriter(file, fieldnames=fields)
                    writer.writeheader()
                    for key in ids:
                        track = library[key]
                        raw = features.get(key)
                        status, musicnn, clap = _coverage(raw)
                        counts[status] += 1
                        writer.writerow({
                            "rating_key": key,
                            "album_rating_key": track.get("album_key") or "",
                            "artist": track.get("artist") or "",
                            "album": track.get("album") or "",
                            "title": track.get("title") or "",
                            "disc": track.get("disc") or "",
                            "track": track.get("index") or "",
                            "year": track.get("year") or "",
                            "duration_ms": track.get("duration_ms") or "",
                            "genres_json": _json(track.get("genres") or []),
                            "plex_metadata_json": _json(track.get("plex_metadata") or {}),
                            "analysis_status": status,
                            "musicnn_status": musicnn,
                            "clap_status": clap,
                            "energy": raw.get("energy") if raw is not None and
                                      raw.get("energy") is not None else "",
                            "tempo": raw.get("tempo") if raw is not None and
                                     raw.get("tempo") is not None else "",
                            "musical_key": raw.get("key") if raw is not None and
                                           raw.get("key") is not None else "",
                            "scale": raw.get("scale") if raw is not None and
                                     raw.get("scale") is not None else "",
                            "mood_vector_json": _json(raw.get("mood_vector")) if raw is not None else "null",
                            "other_features_json": _json(raw.get("other_features")) if raw is not None else "null",
                            "audio_analysis_raw_json": _json(_raw_scalars(raw)),
                        })
            with archive.open("embeddings.jsonl", "w") as binary:
                with io.TextIOWrapper(binary, encoding="utf-8", newline="") as file:
                    for key in ids + unknown:
                        raw = features.get(key)
                        status, mstate, cstate = _coverage(raw)
                        musicnn, _ = _vector((raw or {}).get("embedding"))
                        clap, _ = _vector((raw or {}).get("clap_embedding"))
                        file.write(_json({
                            "rating_key": key,
                            "in_plex_library": key in library,
                            "analysis_status": status,
                            "musicnn_status": mstate,
                            "clap_status": cstate,
                            "musicnn_embedding": musicnn,
                            "clap_embedding": clap,
                            "musicnn_base64": (raw or {}).get("embedding"),
                            "clap_base64": (raw or {}).get("clap_embedding"),
                            "audio_analysis_raw": raw,
                        }) + "\n")
            # Saved YAML and live Plex are distinct sources. Both appear
            # alongside one another so proposals can use the actual current
            # order rather than assuming the YAML was last synchronized.
            archive.writestr("playlists.yml", yaml.safe_dump(
                {"playlists": definitions,
                 "live_plex_playlists": live_playlists or {}},
                sort_keys=False, allow_unicode=True, default_flow_style=False))
            manifest = {
                "format": "plex-music-collaboration-export",
                "schema_version": EXPORT_VERSION,
                "created_at": _now(),
                "library_updated_at": catalogue_updated,
                "audio_last_sync": cache_state.get("last_sync"),
                "audio_manifest_version": cache_state.get("manifest_version"),
                "audio_sync_pending": cache_state.get("pending"),
                "audio_sync_available": cache_state.get("available"),
                "library_revision": catalogue_revision(library),
                "playlist_revision": fingerprint(definitions),
                "audio_revision": feature_revision(features,cache_state.get("manifest_version")),
                "live_plex_playlists": {
                    "status": "included" if live_playlists is not None else "unavailable",
                    "count": len(live_playlists or {}),
                    "note": "Live Plex playlists were unavailable at export time" if live_playlists is None else
                            "Current Plex playlist order, not necessarily synchronized with saved YAML",
                },
                "files": ["tracks.csv", "embeddings.jsonl", "playlists.yml", "manifest.json"],
                "counts": counts,
                "coverage_definition": {
                    "missing_analysis": "No AudioMuse cache record exists for this Plex ID",
                    "no_embeddings": "Record exists but neither embedding exists",
                    "partial_embeddings": "Only one usable audio embedding exists",
                    "invalid_embeddings": "An embedding is malformed or non-finite",
                    "complete_embeddings": "Both MusiCNN and CLAP embeddings decode",
                },
                "proposal_format": PROPOSAL_FORMAT,
                "proposal_instructions": "Return a proposal.json with format, source.library_revision, source.playlist_revision, and playlists of {name,action,rating_keys,description(optional for create),notes(optional)}. Actions are create or update.",
                "notes": [
                    "The library is the last complete Plex catalogue snapshot, not an instantaneous live scan",
                    "AudioMuse raw values and original base64 embeddings are preserved exactly",
                    "Decoded embedding arrays are full-length unnormalized float32 measurements",
                    "Non-finite upstream numeric values are marked explicitly as __nonfinite_float__",
                    "Extra cached AudioMuse records not in the Plex catalogue are retained in embeddings.jsonl",
                    "playlists.yml has both saved definitions and live_plex_playlists when reachable",
                    "An unavailable live Plex snapshot is explicitly identified in the manifest",
                    "Legacy Plex catalogues may lack enriched raw tags until the next background rescan; the count is explicit",
                    "No audio media bytes are exported; raw metadata may contain source paths or other personal metadata; inspect the ZIP before sharing",
                ],
            }
            archive.writestr("manifest.json", json.dumps(
                manifest, ensure_ascii=False, indent=2, default=str))
    except Exception:
        os.unlink(filename)
        raise
    return Path(filename), manifest


def decode_proposal(data: bytes, content_type: str) -> dict:
    if not data or len(data) > MAX_PROPOSAL_BYTES:
        raise CollaborationError("Proposal must be 1 byte to 2 MiB.")
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                files = [f for f in archive.infolist() if f.filename == "proposal.json"]
                if len(files) != 1 or files[0].file_size > MAX_PROPOSAL_BYTES:
                    raise CollaborationError("ZIP must contain one proposal.json (2 MiB maximum).")
                data = archive.read(files[0])
        except zipfile.BadZipFile as exc:
            raise CollaborationError("Invalid ZIP archive.") from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CollaborationError("Proposal must be valid UTF-8 JSON.") from exc
    return normalize_proposal(payload)


def normalize_proposal(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) != {"format", "source", "playlists"}:
        raise CollaborationError("Expected format, source and playlists fields.")
    if data["format"] != PROPOSAL_FORMAT:
        raise CollaborationError("Unsupported proposal format; use plex-playlist-proposal-v1.")
    src = data["source"]
    if not isinstance(src, dict) or not all(
        isinstance(src.get(k), str) and re.fullmatch(r"[0-9a-f]{12,64}", src[k])
        for k in ("library_revision", "playlist_revision")
    ):
        raise CollaborationError("Copy the two revision IDs from manifest.json into source.")
    if not isinstance(data["playlists"], list) or not 1 <= len(data["playlists"]) <= MAX_PROPOSALS:
        raise CollaborationError("Provide 1–30 proposed playlists.")
    out = []
    seen = set()
    for n, item in enumerate(data["playlists"], start=1):
        if not isinstance(item, dict) or (set(item) -
             {"name", "action", "rating_keys", "description", "notes"}):
            raise CollaborationError(f"Playlist {n} has invalid or unsupported fields.")
        name = item.get("name")
        try:
            name = _name(name)
        except ValueError as exc:
            raise CollaborationError(str(exc)) from exc
        if name.casefold() in seen or name in DEFAULT_HIDDEN_PLAYLIST_TITLES:
            raise CollaborationError(f"Duplicate or protected playlist name: {name}")
        seen.add(name.casefold())
        action = item.get("action")
        if action not in ("create", "update"):
            raise CollaborationError(f"{name}: action must be create or update.")
        keys = item.get("rating_keys")
        limit = 120 if action == "create" else 1000
        if (not isinstance(keys, list) or not 1 <= len(keys) <= limit
            or any(not isinstance(k, str) or not k.isascii() or
                   not k.isdecimal() or int(k) < 1 or str(int(k)) != k
                   for k in keys)
            or len(set(keys)) != len(keys)):
            raise CollaborationError(f"{name}: provide 1–{limit} unique, exact Plex track ID strings.")
        description = item.get("description")
        notes = item.get("notes")
        if description is not None and (not isinstance(description, str)
                                        or len(description) > MAX_DESC):
            raise CollaborationError(f"{name}: description must be text under 2000 characters.")
        if notes is not None and (not isinstance(notes, str) or len(notes) > MAX_DESC):
            raise CollaborationError(f"{name}: notes must be text under 2000 characters.")
        out.append({"name": name,"action": action,"rating_keys": keys,
                    "description": description, "notes": notes})
    return {"format": PROPOSAL_FORMAT,
            "source": {
                "library_revision": src["library_revision"],
                "playlist_revision": src["playlist_revision"],
            }, "playlists": out}


def _signature(payload: dict) -> str:
    value = _json(payload).encode("utf-8")
    return hmac.new(_SECRET, value, hashlib.sha256).hexdigest()


def preview_proposal(proposal: dict, library: dict, definitions: dict, plex) -> dict:
    """Preview exact live Plex changes; no YAML, Plex or draft writes."""
    proposal = normalize_proposal(proposal)
    source = proposal["source"]
    if source["library_revision"] != catalogue_revision(library):
        raise CollaborationError("Plex catalogue differs from the export. Download a new ZIP.")
    if source["playlist_revision"] != fingerprint(definitions):
        raise CollaborationError("Saved playlists changed since export. Download a new ZIP.")
    live = plex.playlists(playlistType="audio")
    by_name = {}
    for item in live:
        by_name.setdefault(str(item.title).casefold(), []).append(item)
    defs_casefold = {name.casefold():name for name in definitions}
    from .audiomuse_client import cached_features
    from .playlist_quality import evaluate
    evidence, _ = cached_features()
    previews=[]
    for item in proposal["playlists"]:
        name, action, keys = item["name"], item["action"], item["rating_keys"]
        missing=[key for key in keys if key not in library]
        if missing:
            raise CollaborationError(f"{name}: missing Plex track IDs: " +
                                     ", ".join(missing[:20]))
        live_matches = by_name.get(name.casefold(), [])
        saved_match = defs_casefold.get(name.casefold())
        if action == "create":
            if live_matches or saved_match is not None:
                raise CollaborationError(f"{name}: playlist exists; use action update.")
            before=[]
            playlist_id=None
        else:
            if saved_match != name or len(live_matches) != 1 or live_matches[0].title != name:
                raise CollaborationError(
                    f"{name}: update requires an exact-name saved YAML and live Plex playlist.")
            existing=live_matches[0]
            if getattr(existing, "smart", False) or getattr(existing, "radio", False):
                raise CollaborationError(f"{name}: smart and radio playlists cannot be edited.")
            if item["description"] is not None and item["description"] != str(
                definitions[name].get("description") or ""):
                raise CollaborationError(
                    f"{name}: updating the description is unsupported; omit it to preserve the original.")
            before=current_keys(existing)
            if len(set(before)) != len(before):
                raise CollaborationError(f"{name}: duplicate tracks in live Plex; review manually.")
            if any(key not in library for key in before):
                raise CollaborationError(f"{name}: live Plex references tracks missing from the index.")
            playlist_id=str(existing.ratingKey)
        changed = keys != before
        before_set, after_set = set(before), set(keys)
        positions = {key:i for i,key in enumerate(before)}
        moved = sum(1 for i,key in enumerate(keys)
                    if key in positions and positions[key] != i)
        previews.append({
            "name":name, "action":action, "status":"changed" if changed else "unchanged",
            "playlist_id":playlist_id, "before_snapshot":signature(before),
            "before_count":len(before), "after_count":len(keys),
            "added":len(after_set-before_set),
            "removed":len(before_set-after_set), "moved":moved,
            "notes":item["notes"],
            "quality":evaluate(keys,library,evidence,definitions,
                               exclude_name=name if action=="update" else None),
            "track_order":[{
                "id":key, "artist":library[key]["artist"], "album":library[key]["album"],
                "title":library[key]["title"],"year":library[key].get("year")
            } for key in keys],
        })
    token=_signature({"proposal":proposal,"previews":previews})
    return {"proposal":proposal, "previews":previews,
            "review_token":token,
            "changes":sum(p["status"]=="changed" for p in previews),
            "untouched_playlists": max(0,len(definitions)-
                                     sum(p["action"]=="update" for p in previews))}


def apply_proposal(proposal: dict, review_token: str,
                   library: dict, plex, *, approved: bool) -> dict:
    """Apply only after a matching, explicit preview approval.

    Multi-playlist apply stops on its first failure and reports applied/
    not-attempted names. Existing identities and covers are preserved.
    """
    if approved is not True:
        raise CollaborationError("Explicit proposal approval required.")
    if not isinstance(review_token, str) or not hmac.compare_digest(
        review_token, review_token.strip()
    ):
        raise CollaborationError("Invalid proposal review token.")
    with PUBLISH_LOCK, _WRITE_LOCK:
        definitions=load().get("playlists", {})
        preview=preview_proposal(proposal,library,definitions,plex)
        if not hmac.compare_digest(review_token,preview["review_token"]):
            raise CollaborationError("Proposal or Plex state changed after preview. Review again.")
        results=[]
        entries=preview["proposal"]["playlists"]
        for i,item in enumerate(entries):
            p=preview["previews"][i]
            name,keys=item["name"],item["rating_keys"]
            if p["status"]=="unchanged":
                results.append({"name":name,"action":item["action"],"status":"unchanged"})
                continue
            try:
                if item["action"]=="update":
                    definitions=load().get("playlists",{})
                    before=current_keys(find_live(plex,name))
                    if signature(before) != p["before_snapshot"]:
                        raise CollaborationError("Plex playlist changed during import.")
                    version=fingerprint(definitions)
                    operations=[{"type":"set_order","rating_keys":keys}]
                    plan=change_plan(before,operations,library,name=name,
                                     version=version,snapshot=signature(before))
                    result=apply_approved(
                        plex,name,version=version,snapshot=signature(before),
                        operations=operations,plan_id=plan["plan_id"],
                        library=library,approved=True,
                        forbidden=DEFAULT_HIDDEN_PLAYLIST_TITLES)
                else:
                    draft=create_draft({
                        "name":name,"keys":keys,
                        "request":{"prompt":item["description"] or "",
                                   "criteria":{"genre":None,"subgenre":None}},
                        "diagnostics":{},"warnings":[],
                        "selection_policy_version":"collaboration-proposal-v1",
                        "source_revisions":{"library":catalogue_revision(library)},
                    })
                    plan=prepare_generation(plex,draft,name,library)
                    result=publish_generation(
                        plex,draft["id"],draft["revision"],name,
                        plan["plan_id"],True,library)
                results.append({"name":name, "action":item["action"], **result})
                if result.get("status") not in ("synced","published"):
                    return {"ok":False, "results":results,
                            "error":f"{name} needs synchronization repair",
                            "not_attempted":[x["name"] for x in entries[i+1:]]}
                if result.get("sync_state") in ("state_repair_required",) or (
                    result.get("status") == "saved_needs_state_repair"
                ):
                    return {"ok":False, "results":results,
                            "error":f"{name} needs state repair",
                            "not_attempted":[x["name"] for x in entries[i+1:]]}
            except Exception as exc:
                return {"ok":False, "results":results,
                        "failed":{"name":name,"reason":str(exc)},
                        "not_attempted":[x["name"] for x in entries[i+1:]]}
        return {"ok":True, "results":results, "not_attempted":[]}
