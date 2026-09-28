from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


PLAYLISTS_FILE = Path(os.getenv("PLAYLISTS_FILE", "/data/playlists.yml"))


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
            artist = str(item.get("artist", "")).strip()
            album = str(item.get("album", "")).strip()
            track = str(item.get("track", "")).strip()
            if not artist or not (album or track):
                raise ValueError(
                    f"Playlist '{name}', item {i}: use artist+album or artist+track."
                )
    return data


def load() -> dict[str, Any]:
    return parse_text(read_text())


def save_text(text: str) -> dict[str, Any]:
    data = parse_text(text)
    ensure_file()
    temp = PLAYLISTS_FILE.with_suffix(PLAYLISTS_FILE.suffix + ".tmp")
    temp.write_text(text.rstrip() + "\n", encoding="utf-8")
    temp.replace(PLAYLISTS_FILE)
    return data
