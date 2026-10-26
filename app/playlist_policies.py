"""Playlist variety policies, stored separately from Plex playlist YAML.

Changing curated/rotating mode must not mark a playlist as needing Plex sync.
"""
from __future__ import annotations

import os
from pathlib import Path
import yaml

POLICIES_FILE = Path(os.getenv(
    "PLAYLIST_POLICIES_FILE",
    str(Path(os.getenv("PLAYLISTS_FILE", "/data/playlists.yml")).with_name("playlist-policies.yml")),
))


def read_policies():
    if not POLICIES_FILE.exists():
        return {}
    raw = yaml.safe_load(POLICIES_FILE.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("playlists", {}), dict):
        raise ValueError("Invalid playlist-policies.yml")
    return {name: kind for name, kind in raw.get("playlists", {}).items()
            if isinstance(name, str) and kind in ("curated", "rotating")}


def set_policy(name, kind, valid_names):
    if name not in valid_names:
        raise ValueError("Unknown playlist")
    if kind not in ("curated", "rotating"):
        raise ValueError("Policy must be curated or rotating")
    data = read_policies()
    if kind == "curated":
        data.pop(name, None)
    else:
        data[name] = kind
    POLICIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    staging = POLICIES_FILE.with_suffix(".yml.tmp")
    staging.write_text(yaml.safe_dump({"playlists": data}, sort_keys=True), encoding="utf-8")
    staging.replace(POLICIES_FILE)
    return kind
