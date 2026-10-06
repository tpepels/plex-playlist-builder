from __future__ import annotations

import os
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


def _read_state() -> dict[str, Any]:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"pending": []}

    try:
        state = yaml.safe_load(STATE_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {"pending": []}

    pending = state.get("pending", [])
    if not isinstance(pending, list):
        pending = []

    normalized = []
    seen = set()
    for name in pending:
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            normalized.append(name)
    return {"pending": normalized}


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
    existing = pending_names()
    pending = [
        name
        for name in changed + existing
        if name in valid
    ]
    _write_state({"pending": list(dict.fromkeys(pending))})


def mark_synced(name: str) -> None:
    pending = [item for item in pending_names() if item != name]
    _write_state({"pending": pending})


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


def merge_text(text: str) -> dict[str, Any]:
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
    return merged
