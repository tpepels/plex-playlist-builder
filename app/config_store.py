from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


PLAYLISTS_FILE = Path(os.getenv("PLAYLISTS_FILE", "/data/playlists.yml"))
STATE_FILE = Path(
    os.getenv(
        "PLAYLIST_STATE_FILE",
        str(PLAYLISTS_FILE.with_name("playlist-state.yml")),
    )
)


def ensure_file() -> None:
    PLAYLISTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not PLAYLISTS_FILE.exists():
        PLAYLISTS_FILE.write_text("playlists: {}\n", encoding="utf-8")


def read_text() -> str:
    ensure_file()
    return PLAYLISTS_FILE.read_text(encoding="utf-8")


def parse_text(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("Top level YAML must be a mapping.")
    playlists = data.get("playlists", {})
    if not isinstance(playlists, dict):
        raise ValueError("'playlists' must be a mapping of playlist names to definitions.")

    for name, spec in playlists.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Playlist names must be non-empty strings.")
        if not isinstance(spec, dict):
            raise ValueError(f"Playlist '{name}' must be a mapping.")
        items = spec.get("items", [])
        if not isinstance(items, list):
            raise ValueError(f"Playlist '{name}': 'items' must be a list.")
        for i, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Playlist '{name}', item {i} must be a mapping.")
            rating_key = str(item.get("rating_key", "")).strip()
            artist = str(item.get("artist", "")).strip()
            album = str(item.get("album", "")).strip()
            track = str(item.get("track", "")).strip()
            if not rating_key and (not artist or not (album or track)):
                raise ValueError(
                    f"Playlist '{name}', item {i}: use rating_key, artist+album, or artist+track."
                )
    return data


def load() -> dict[str, Any]:
    return parse_text(read_text())


def fingerprint(value: Any) -> str:
    """A stable content ID, independent of YAML whitespace and mapping order."""
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_state() -> dict[str, Any]:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"pending": [], "synced_versions": {}}
    try:
        state = yaml.safe_load(STATE_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        state = {}
    if not isinstance(state, dict):
        state = {}

    pending = state.get("pending", [])
    if not isinstance(pending, list):
        pending = []
    state["pending"] = list(dict.fromkeys(name for name in pending if isinstance(name, str) and name))
    if not isinstance(state.get("synced_versions"), dict):
        state["synced_versions"] = {}
    if not isinstance(state.get("last_import"), dict):
        state["last_import"] = None
    return state


def _write_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temp.write_text(
        yaml.safe_dump(state, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temp.replace(STATE_FILE)


def pending_names(valid_names: Iterable[str] | None = None) -> list[str]:
    pending = list(_read_state().get("pending", []))
    if valid_names is None:
        return pending
    valid = set(valid_names)
    return [name for name in pending if name in valid]


def _update_pending(changed_names: Iterable[str], valid_names: Iterable[str]) -> None:
    changed = list(dict.fromkeys(changed_names))
    valid = set(valid_names)
    state = _read_state()
    existing = state["pending"]
    state["pending"] = list(dict.fromkeys(name for name in changed + existing if name in valid))
    state["synced_versions"] = {
        name: revision for name, revision in state["synced_versions"].items() if name in valid
    }
    if changed:
        state["last_changed_at"] = _now()
    _write_state(state)


def mark_synced(name: str, spec: dict[str, Any] | None = None) -> None:
    state = _read_state()
    state["pending"] = [item for item in state["pending"] if item != name]
    if spec is not None:
        state["synced_versions"][name] = fingerprint(spec)
        state["last_synced_at"] = _now()
    _write_state(state)


def version_details(definitions: dict[str, Any]) -> dict[str, Any]:
    """Report what is actually loaded; don't infer whether a file is chronologically newer."""
    state = _read_state()
    pending = set(state["pending"])
    synced = state["synced_versions"]
    statuses: dict[str, str] = {}
    for name, spec in definitions.items():
        saved_hash = synced.get(name)
        current_hash = fingerprint(spec)
        if saved_hash and saved_hash == current_hash:
            statuses[name] = "synced"
        elif name in pending or saved_hash:
            statuses[name] = "needs_sync"
        else:
            statuses[name] = "unverified"

    loaded_id = fingerprint(definitions)
    last_import = state.get("last_import")
    if last_import:
        last_import = {**last_import, "matches_loaded": last_import.get("loaded_id") == loaded_id}
    try:
        changed_at = datetime.fromtimestamp(
            PLAYLISTS_FILE.stat().st_mtime, timezone.utc
        ).isoformat(timespec="seconds")
    except OSError:
        changed_at = None

    return {
        "id": loaded_id,
        "file_modified_at": changed_at,
        "last_import": last_import,
        "sync_statuses": statuses,
        "counts": {
            "total": len(definitions),
            "needs_sync": sum(status == "needs_sync" for status in statuses.values()),
            "synced": sum(status == "synced" for status in statuses.values()),
            "unverified": sum(status == "unverified" for status in statuses.values()),
        },
    }


def inspect_import(text: str) -> dict[str, Any]:
    """Compare an incoming pack without modifying the saved configuration."""
    incoming = parse_text(text).get("playlists", {})
    current = load().get("playlists", {})
    new = [name for name in incoming if name not in current]
    updated = [name for name in incoming if name in current and current[name] != incoming[name]]
    unchanged = [name for name in incoming if name in current and current[name] == incoming[name]]
    retained = [name for name in current if name not in incoming]
    merged = {**current, **incoming}
    return {
        "new": new,
        "updated": updated,
        "unchanged": unchanged,
        "retained": retained,
        "already_loaded": not new and not updated,
        "current_id": fingerprint(current),
        "incoming_id": fingerprint(incoming),
        "result_id": fingerprint(merged),
        "result_count": len(merged),
    }


def _write_config_text(text: str) -> None:
    ensure_file()
    temp = PLAYLISTS_FILE.with_suffix(PLAYLISTS_FILE.suffix + ".tmp")
    temp.write_text(text.rstrip() + "\n", encoding="utf-8")
    temp.replace(PLAYLISTS_FILE)


def save_text(text: str) -> dict[str, Any]:
    current = load()
    data = parse_text(text)

    current_playlists = current.get("playlists", {})
    new_playlists = data.get("playlists", {})
    changed = [
        name
        for name, spec in new_playlists.items()
        if current_playlists.get(name) != spec
    ]

    _write_config_text(text)
    _update_pending(changed, new_playlists.keys())
    return data


def merge_text(text: str, source_filename: str | None = None) -> dict[str, Any]:
    incoming = parse_text(text)
    current = load()

    current_playlists = current.get("playlists", {})
    incoming_playlists = incoming.get("playlists", {})
    changed = [
        name
        for name, spec in incoming_playlists.items()
        if current_playlists.get(name) != spec
    ]

    merged = dict(current)
    merged_playlists = dict(current_playlists)
    merged_playlists.update(incoming_playlists)
    merged["playlists"] = merged_playlists

    rendered = yaml.safe_dump(
        merged,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    _write_config_text(rendered)
    _update_pending(changed, merged_playlists.keys())

    state = _read_state()
    state["last_import"] = {
        "filename": Path(source_filename).name[:255] if source_filename else "unnamed YAML pack",
        "imported_at": _now(),
        "source_id": fingerprint(incoming_playlists),
        "loaded_id": fingerprint(merged_playlists),
        "new_count": sum(name not in current_playlists for name in incoming_playlists),
        "updated_count": len(changed) - sum(name not in current_playlists for name in incoming_playlists),
    }
    _write_state(state)
    return merged
