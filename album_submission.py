"""Deterministic identity and audit limits for trusted LAN album submissions."""
from __future__ import annotations

from hashlib import sha256
import re
from urllib.parse import parse_qs, urlparse


def canonical_album_id(url: str) -> str | None:
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != "music.youtube.com":
        return None
    if parsed.path == "/playlist":
        ids = parse_qs(parsed.query).get("list", [])
        if len(ids) == 1 and re.fullmatch(r"OLAK5uy_[A-Za-z0-9_-]+", ids[0]):
            return "playlist:" + ids[0]
    if not parsed.query and re.fullmatch(r"/browse/MPRE[A-Za-z0-9_-]+", parsed.path):
        return "browse:" + parsed.path.rsplit("/", 1)[-1]
    return None


def submission_key(url: str) -> str | None:
    identity = canonical_album_id(url)
    if identity is None:
        return None
    return sha256(("album-download-v1:" + identity).encode("utf-8")).hexdigest()


def clean_match_audit(payload):
    """Permit a bounded, declarative audit; never store arbitrary user objects."""
    if not isinstance(payload, dict):
        return {}
    result = {}
    for key in ("requested_artist", "requested_album", "matched_artist",
                "matched_album", "match_type", "catalog_url"):
        value = payload.get(key)
        if isinstance(value, str):
            result[key] = value[:300]
    for key in ("normalization_rules", "queries"):
        value = payload.get(key)
        if isinstance(value, list):
            result[key] = [part[:200] for part in value[:15] if isinstance(part, str)]
    return result
