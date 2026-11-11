"""Read-only AudioMuse sync to a persistent SQLite feature cache.

AudioMuse is the analysis source; playlist YAML and Plex are never modified here.
"""
from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timezone
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CACHE_FILE = Path(os.getenv("AUDIOMUSE_CACHE_FILE", "/data/audiomuse-cache.sqlite"))
TOKEN_FILE = Path(os.getenv("AUDIOMUSE_API_TOKEN_FILE", "/run/secrets/audiomuse_api_token"))
MAX_INDEX_PAGES = 100
MAX_CHANGED_TRACKS = 50000
BATCH_SIZE = 100
MAX_PER_REQUEST = 500  # Keep a manual UI sync bounded as the library grows.


def _settings() -> tuple[str, str]:
    url = os.getenv("AUDIOMUSE_URL", "").rstrip("/")
    if not url or not url.startswith(("http://", "https://")):
        raise RuntimeError("Configure AUDIOMUSE_URL to reach the AudioMuse web service.")
    token = os.getenv("AUDIOMUSE_API_TOKEN", "").strip()
    if not token:
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                "AudioMuse token unavailable. Mount your existing token file read-only "
                "and set AUDIOMUSE_API_TOKEN_FILE."
            ) from exc
    if not token:
        raise RuntimeError("AudioMuse API token is empty.")
    return url, token


def _get(url: str, token: str, params: dict[str, str | int]) -> dict:
    endpoint = url + "/api/sync?" + urlencode(params)
    request = Request(endpoint, headers={"Accept": "application/json",
                                         "Authorization": "Bearer " + token})
    try:
        with urlopen(request, timeout=25) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"AudioMuse HTTP {exc.code} reading /api/sync") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"AudioMuse sync request failed: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
        raise RuntimeError("Unexpected AudioMuse sync response (missing tracks).")
    if data.get("provider_type") != "plex":
        raise RuntimeError("AudioMuse must be configured with the Plex provider.")
    return data


def _open_cache() -> sqlite3.Connection:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(CACHE_FILE, timeout=10)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS audio_features "
        "(track_id TEXT PRIMARY KEY, fp TEXT NOT NULL, data TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.commit()
    return connection


def _get_index(url: str, token: str) -> dict[str, str]:
    index = {}
    page = 1
    while page <= MAX_INDEX_PAGES:
        reply = _get(url, token, {"fields": "index", "page": page, "limit": 1000})
        for item in reply["tracks"]:
            if not isinstance(item, dict) or not item.get("id") or not item.get("fp"):
                raise RuntimeError("Malformed AudioMuse index entry.")
            track_id, fp = str(item["id"]), str(item["fp"])
            if track_id in index:
                raise RuntimeError("Duplicate AudioMuse provider ID in index.")
            index[track_id] = fp
        if not reply.get("has_more"):
            if len(index) != reply.get("total_tracks"):
                raise RuntimeError("AudioMuse manifest count changed during sync; retry.")
            return index
        following = reply.get("next_page")
        if not isinstance(following, int) or following != page + 1:
            raise RuntimeError("Invalid AudioMuse index page.")
        page = following
    raise RuntimeError("AudioMuse index exceeded safety page limit.")


def _chunks(values: list[str], size: int = BATCH_SIZE):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def synchronize() -> dict:
    """Retrieve changed analysis records atomically; never touch Plex or YAML."""
    url, token = _settings()
    index = _get_index(url, token)
    with closing(_open_cache()) as conn:
        stored = dict(conn.execute("SELECT track_id, fp FROM audio_features"))
        all_changed = [key for key, fp in index.items() if stored.get(key) != fp]
        removed = [key for key in stored if key not in index]
        if len(all_changed) > MAX_CHANGED_TRACKS:
            raise RuntimeError("AudioMuse change set exceeds safety limit.")
        changed = all_changed[:MAX_PER_REQUEST]
        pending = len(all_changed) - len(changed)
        updated = []
        for batch in _chunks(changed):
            reply = _get(url, token, {"ids": ",".join(batch),
                                      "include_embeddings": "true"})
            details = {str(track.get("id")): track for track in reply["tracks"]
                       if isinstance(track, dict)}
            if set(details) != set(batch):
                raise RuntimeError("AudioMuse returned a partial/different track batch; cache unchanged.")
            for track_id in batch:
                track = details[track_id]
                if track.get("fp") != index[track_id]:
                    raise RuntimeError("AudioMuse track changed during sync; cache unchanged.")
                # A track with no embedding is still useful for coverage statistics.
                features = {key: track.get(key) for key in
                            ("title", "artist", "album", "energy", "tempo",
                             "key", "scale", "mood_vector", "other_features",
                             "embedding", "clap_embedding")}
                updated.append((track_id, index[track_id],
                                json.dumps(features, separators=(",", ":"), ensure_ascii=False)))
        # SQLite changes are committed only after every API call succeeded.
        with conn:
            conn.executemany(
                "INSERT INTO audio_features(track_id,fp,data) VALUES(?,?,?) "
                "ON CONFLICT(track_id) DO UPDATE SET fp=excluded.fp,data=excluded.data",
                updated,
            )
            conn.executemany("DELETE FROM audio_features WHERE track_id=?",
                             [(key,) for key in removed])
            meta = {
                "last_sync": datetime.now(timezone.utc).isoformat(),
                "available": str(len(index)),
                "pending": str(pending),
                "manifest_version": hashlib.sha256(
                    json.dumps(index, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            conn.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                list(meta.items()),
            )
        count = conn.execute("SELECT COUNT(*) FROM audio_features").fetchone()[0]
        return {"available": len(index), "new_or_updated": len(changed),
                "removed": len(removed), "cached": count,
                "pending": pending, "complete": pending == 0}


def cached_features() -> tuple[dict[str, dict], dict]:
    if not CACHE_FILE.is_file():
        return {}, {"cached": 0, "last_sync": None}
    with closing(_open_cache()) as conn:
        tracks = {track_id: json.loads(payload)
                  for track_id, payload in conn.execute("SELECT track_id,data FROM audio_features")}
        meta = dict(conn.execute("SELECT key,value FROM metadata"))
        return tracks, {
            "cached": len(tracks),
            "last_sync": meta.get("last_sync"),
            "available": int(meta["available"]) if "available" in meta else None,
            "pending": int(meta["pending"]) if "pending" in meta else None,
            "manifest_version": meta.get("manifest_version"),
        }

def feature_revision(cache: dict[str, dict], manifest_version: str | None = None) -> str:
    """Deterministic revision of evidence consumed by a generation draft.

    Include values, not just track IDs, so an AudioMuse reanalysis invalidates
    the original source snapshot. Sorting makes this stable across cache reads.
    """
    content = json.dumps(cache, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(((manifest_version or "") + content).encode()).hexdigest()


def readiness(library: dict, cache: dict, state: dict) -> dict:
    """Reconcile the local Plex catalogue and the last observed AudioMuse manifest.

    This is a local snapshot: live upstream completion is checked by the Sync
    action. Report unknown manifest state explicitly, not as completed analysis.
    """
    from .playlist_curator import features_for
    ids = set(str(k) for k in library)
    indexed = set(str(k) for k in cache)
    matching = ids & indexed
    decoded = features_for({k: cache[k] for k in matching})
    present = lambda value: value is not None
    valid_musicnn = sum(present(decoded[k].get("musicnn")) for k in matching)
    valid_clap = sum(present(decoded[k].get("clap")) for k in matching)
    energy = sum(present(decoded[k].get("energy")) for k in matching)
    tempo = sum(present(decoded[k].get("tempo")) for k in matching)
    mood = sum(bool(decoded[k].get("mood")) for k in matching)
    invalid_musicnn = sum(
        cache[k].get("embedding") is not None and decoded[k].get("musicnn") is None
        for k in matching
    )
    invalid_clap = sum(
        cache[k].get("clap_embedding") is not None and decoded[k].get("clap") is None
        for k in matching
    )
    analyzed = sum(
        decoded[k].get("musicnn") is not None or decoded[k].get("clap") is not None
        for k in matching
    )
    pending = state.get("pending")
    available = state.get("available")
    return {
        "plex_tracks": len(ids),
        "audiomuse_available": available,
        "cached_tracks": len(indexed),
        "matched_track_ids": len(matching),
        "audio_analyzed_tracks": analyzed,
        "missing_embeddings": len(ids)-analyzed,
        "unmatched_plex_tracks": len(ids - indexed),
        "orphan_audio_tracks": len(indexed - ids),
        "musicnn_available": valid_musicnn,
        "clap_available": valid_clap,
        "energy_available": energy,
        "tempo_available": tempo,
        "mood_available": mood,
        "invalid_musicnn": invalid_musicnn,
        "invalid_clap": invalid_clap,
        "pending": pending,
        "last_sync": state.get("last_sync"),
        "manifest_version": state.get("manifest_version"),
        "ready": bool(
            ids and available is not None and pending == 0
            and len(matching) == len(ids)
            and analyzed == len(ids)
            and invalid_musicnn == 0 and invalid_clap == 0
        ),
        "basis": "last_successful_local_sync",
        "warning": (
            "This is the last synchronized manifest, not a live upstream task status."
        ),
    }
