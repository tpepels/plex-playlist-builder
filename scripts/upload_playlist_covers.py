#!/usr/bin/env python3
"""Upload Plex music playlist covers by exact filename, safely previewing first.

Place '3 AM.jpg' or 'Morning - Clear Head.png' in the covers folder.
  python3 scripts/upload_playlist_covers.py --plan
  python3 scripts/upload_playlist_covers.py --apply

Only playlist posters change. No changes to the playlist manager are required.
Python 3 standard library only. Uses PLEX_TOKEN or Plex Preferences.xml.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_COVERS = Path("/srv/complete/plex-playlist-covers")
DEFAULT_PLEX = "http://127.0.0.1:32400"
MAX_IMAGE_BYTES = 15 * 1024 * 1024
PREFERENCE_PATHS = (
    "/srv/plex/config/Library/Application Support/Plex Media Server/Preferences.xml",
    "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml",
    "/plex-config/Library/Application Support/Plex Media Server/Preferences.xml",
)
SUPPORTED = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


@dataclass(frozen=True)
class Playlist:
    title: str
    rating_key: str


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Plex redirected a request; refusing to forward its access token")


def name_key(value: str) -> str:
    """Only Unicode canonical equivalence; no fuzzy/case-insensitive matching."""
    return unicodedata.normalize("NFC", value)


def validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Use an explicit http(s) Plex URL, e.g. http://localhost:32400")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Plex URL must not contain credentials, paths or query parameters")
    return value.rstrip("/")


def load_token(preferences: str | None = None) -> str:
    token = os.environ.get("PLEX_TOKEN", "").strip()
    if token:
        return token
    configured = preferences or os.environ.get("PLEX_PREFERENCES", "").strip()
    paths = [configured] if configured else PREFERENCE_PATHS
    for filename in paths:
        path = Path(filename)
        if not path.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
            token = root.attrib.get("PlexOnlineToken", "").strip()
        except (OSError, ET.ParseError) as exc:
            raise ValueError(f"Cannot read Plex preferences at {path}: {exc}") from exc
        if token:
            return token
    raise ValueError("No Plex token found. Set PLEX_TOKEN or use --preferences /path/to/Preferences.xml")


def get_playlists(server: str, token: str) -> list[Playlist]:
    opener = urllib.request.build_opener(NoRedirect)
    results: list[Playlist] = []
    seen_ids: set[str] = set()
    start, count = 0, 200
    while True:
        query = urllib.parse.urlencode({"playlistType": "audio"})
        url = f"{server}/playlists?{query}"
        request = urllib.request.Request(url, headers={
            "X-Plex-Token": token,
            "Accept": "application/xml",
            "X-Plex-Container-Start": str(start),
            "X-Plex-Container-Size": str(count),
        })
        with opener.open(request, timeout=20) as response:
            document = response.read(4 * 1024 * 1024 + 1)
            if len(document) > 4 * 1024 * 1024:
                raise ValueError("Plex playlist response was unexpectedly large")
        root = ET.fromstring(document)
        rows = list(root.findall("Playlist")) + list(root.findall("Metadata"))
        before = len(seen_ids)
        for element in rows:
            if element.get("playlistType") != "audio":
                continue
            key = element.get("ratingKey", "")
            title = element.get("title", "")
            if not title or not re.fullmatch(r"\d+", key) or key in seen_ids:
                continue
            seen_ids.add(key)
            results.append(Playlist(title, key))
        try:
            total = int(root.attrib.get("totalSize", "-1"))
        except ValueError:
            total = -1
        start += len(rows)
        # Some Plex servers return fewer items than requested on each page.
        if not rows or len(seen_ids) == before:
            break
        if total >= 0:
            if start >= total:
                break
        elif len(rows) < count:
            break
    return results


def image_type(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("not a regular image file")
    expected = SUPPORTED.get(path.suffix.lower())
    if not expected:
        raise ValueError("unsupported image extension")
    size = path.stat().st_size
    if not 0 < size <= MAX_IMAGE_BYTES:
        raise ValueError("image must be between 1 byte and 15 MiB")
    with path.open("rb") as image:
        signature = image.read(24)
    actual = ("image/jpeg" if signature.startswith(b"\xff\xd8\xff") else
              "image/png" if signature.startswith(b"\x89PNG\r\n\x1a\n") else None)
    if actual != expected:
        raise ValueError("file contents do not match its JPG/PNG extension")
    return expected


def find_matches(covers: Path, playlists: list[Playlist]):
    if not covers.is_dir():
        raise ValueError(f"Cover directory does not exist: {covers}")
    by_title: dict[str, list[Playlist]] = defaultdict(list)
    for playlist in playlists:
        by_title[name_key(playlist.title)].append(playlist)
    by_image: dict[str, list[Path]] = defaultdict(list)
    for image in sorted(covers.iterdir()):
        if image.suffix.lower() in SUPPORTED and (image.is_file() or image.is_symlink()):
            by_image[name_key(image.stem)].append(image)
    matches: list[tuple[Playlist, Path, str]] = []
    warnings: list[str] = []
    for title, images in sorted(by_image.items()):
        candidates = by_title.get(title, [])
        if not candidates:
            warnings.append(f'NO PLEX MATCH: {", ".join(p.name for p in images)}')
        elif len(images) != 1:
            warnings.append(f'AMBIGUOUS COVER: {title} ({", ".join(p.name for p in images)})')
        elif len(candidates) != 1:
            warnings.append(f"AMBIGUOUS PLAYLIST: {title} ({len(candidates)} matching audio playlists)")
        else:
            try:
                mime = image_type(images[0])
            except (OSError, ValueError) as exc:
                warnings.append(f"INVALID IMAGE: {images[0].name} ({exc})")
                continue
            matches.append((candidates[0], images[0], mime))
    return matches, warnings


def upload(server: str, token: str, playlist: Playlist, image: Path, mime: str):
    # Plex's poster API, used by python-plexapi's PosterMixin.uploadPoster.
    endpoint = f"{server}/library/metadata/{playlist.rating_key}/posters"
    payload = image.read_bytes()
    if len(payload) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image changed or exceeded the size limit: {image}")
    request = urllib.request.Request(endpoint, data=payload, method="POST", headers={
        "X-Plex-Token": token,
        "Content-Type": mime,
        "Accept": "application/xml",
    })
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=45) as response:
        if not 200 <= response.status < 300:
            raise ValueError(f"Unexpected status HTTP {response.status}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--covers", type=Path, default=DEFAULT_COVERS,
                        help=f"Local JPG/PNG files named exactly like Plex playlists (default: {DEFAULT_COVERS})")
    parser.add_argument("--plex", default=os.environ.get("PLEX_URL", DEFAULT_PLEX),
                        help=f"Plex server URL (default: {DEFAULT_PLEX})")
    parser.add_argument("--preferences", help="Plex Preferences.xml location (if PLEX_TOKEN is not set)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Preview only (default)")
    mode.add_argument("--apply", action="store_true", help="Upload matched covers to Plex")
    args = parser.parse_args(argv)
    try:
        base = validate_base_url(args.plex)
        if not args.covers.is_dir():
            raise ValueError(f"Cover directory does not exist: {args.covers}")
        token = load_token(args.preferences)
        playlists = get_playlists(base, token)
        matches, warnings = find_matches(args.covers, playlists)
        print(f"Plex audio playlists: {len(playlists)} · cover matches: {len(matches)}")
        for playlist, image, _ in matches:
            print(f'{"UPLOAD" if args.apply else "PREVIEW"}: {playlist.title} [Plex {playlist.rating_key}] <- {image.name}')
        for warning in warnings:
            print("SKIP:", warning)
        if not args.apply:
            print("\nDry run: nothing was changed. Use --apply to upload these images.")
            return 0
        if not matches:
            print("No unambiguous, valid covers to upload.")
            return 1
        failures = 0
        for playlist, image, mime in matches:
            try:
                upload(base, token, playlist, image, mime)
                print(f"OK: {playlist.title}")
            except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError) as exc:
                print(f"FAILED: {playlist.title}: {exc}", file=sys.stderr)
                failures += 1
        print(f"Finished: {len(matches) - failures}/{len(matches)} uploaded. Unmatched playlists unchanged.")
        return 1 if failures else 0
    except (OSError, ValueError, ET.ParseError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
