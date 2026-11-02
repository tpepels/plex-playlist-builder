#!/usr/bin/env python3
"""Read-only AudioMuse manifest probe against Plex's exported rating keys."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


Progress = Callable[[str], None]


def show_progress(message: str) -> None:
    print(f"[AudioMuse probe] {message}", file=sys.stderr, flush=True)


def plex_ids_from_tsv(content: str) -> set[str]:
    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    if not reader.fieldnames or "rating_key" not in reader.fieldnames:
        raise ValueError("Plex TSV is missing the rating_key column")
    return {str(row["rating_key"]).strip() for row in reader if row.get("rating_key", "").strip()}


def plex_ids(path: Path) -> set[str]:
    return plex_ids_from_tsv(path.read_text(encoding="utf-8-sig"))


def plex_ids_from_manager(url: str, *, progress: Progress | None = None) -> set[str]:
    """Download current Plex rating keys from our existing read-only endpoint."""
    endpoint = url.rstrip("/") + "/api/library-export.tsv"
    if progress:
        progress(f"Requesting Plex full-library TSV from {endpoint} (can be slow)")
    try:
        with urlopen(Request(endpoint, headers={"Accept": "text/tab-separated-values"}), timeout=90) as response:
            content = response.read().decode("utf-8-sig")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Plex library export failed at {endpoint}: {exc}") from exc
    ids = plex_ids_from_tsv(content)
    if progress:
        progress(f"Plex full-library export completed: {len(ids)} track IDs")
    return ids


def load_api_token() -> str:
    """Load a token without placing credentials in Git or shell history.

    Explicit AUDIOMUSE_API_TOKEN wins. Otherwise read the local user-only
    token file, optionally configured through AUDIOMUSE_API_TOKEN_FILE.
    """
    token = os.environ.get("AUDIOMUSE_API_TOKEN", "").strip()
    if token:
        return token

    explicit = os.environ.get("AUDIOMUSE_API_TOKEN_FILE", "").strip()
    token_path = Path(explicit).expanduser() if explicit else (
        Path.home() / ".config" / "plex-playlist-manager" / "audiomuse-api-token"
    )
    if not token_path.exists():
        if explicit:
            raise RuntimeError(f"AudioMuse token file does not exist: {token_path}")
        return ""
    if not token_path.is_file():
        raise RuntimeError(f"AudioMuse token path is not a regular file: {token_path}")
    if token_path.is_symlink():
        raise RuntimeError(f"AudioMuse token file cannot be a symlink: {token_path}")
    if token_path.stat().st_mode & 0o077:
        raise RuntimeError(
            f"AudioMuse token file permissions are too open; run chmod 600 {token_path}"
        )
    return token_path.read_text(encoding="utf-8").strip()


def auth_headers(*, token: str = "", cookie: str = "") -> dict[str, str]:
    """Use AudioMuse's documented machine-to-machine Bearer token when set."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif cookie:
        headers["Cookie"] = cookie
    return headers


def get_manifest(url: str, *, token: str = "", cookie: str = "", all_pages: bool = False, progress: Progress | None = None) -> dict:
    tracks: list[dict] = []
    page = 1
    total = 0
    provider = None
    while True:
        endpoint = url.rstrip("/") + "/api/sync?" + urlencode({"fields": "index", "page": page, "limit": 1000})
        headers = auth_headers(token=token, cookie=cookie)
        if progress:
            progress(f"Requesting AudioMuse manifest page {page} (limit=1000)")
        try:
            with urlopen(Request(endpoint, headers=headers), timeout=15) as response:
                result = json.load(response)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError("Authentication required: configure AUDIOMUSE_API_TOKEN or a protected ~/.config/plex-playlist-manager/audiomuse-api-token file (never share it)") from exc
            raise RuntimeError(f"AudioMuse HTTP {exc.code} on /api/sync") from exc
        except URLError as exc:
            raise RuntimeError(f"Cannot reach AudioMuse at {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"AudioMuse manifest page {page} timed out") from exc
        if not isinstance(result, dict) or not isinstance(result.get("tracks"), list):
            raise RuntimeError("Unexpected AudioMuse response, expected tracks[]")
        if not all(isinstance(t, dict) and t.get("id") is not None and "fp" in t for t in result["tracks"]):
            raise RuntimeError("Unexpected AudioMuse manifest entry, expected id and fp")
        tracks.extend(result["tracks"])
        total = result.get("total_tracks", total)
        provider = result.get("provider_type", provider)
        if progress:
            progress(f"Received AudioMuse page {page}: {len(result['tracks'])} entries; {len(tracks)} total fetched (server reports {total})")
        if not all_pages or not result.get("has_more"):
            break
        next_page = result.get("next_page")
        if not isinstance(next_page, int) or next_page <= page:
            raise RuntimeError("Invalid next_page from AudioMuse")
        page = next_page
    return {"tracks": tracks, "total": total, "provider": provider, "pages": page}


def compare(manifest: dict, plex: set[str] | None) -> dict:
    ids = [str(t["id"]) for t in manifest["tracks"]]
    output = {
        "provider_type": manifest["provider"],
        "analyzed_tracks_reported": manifest["total"],
        "manifest_tracks_fetched": len(ids),
        "manifest_pages_fetched": manifest["pages"],
        "sample_audio_ids": ids[:5],
    }
    if plex is not None:
        matched = set(ids) & plex
        output.update({
            "plex_library_tracks": len(plex),
            "exact_plex_rating_key_matches": len(matched),
            "match_percentage_of_fetched": round(100 * len(matched) / len(ids), 1) if ids else None,
            "unmatched_sample": [i for i in ids if i not in plex][:5],
            "mapping_validated": bool(matched),
        })
    return output



def _normalise_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _sample_ids(manifest: dict, limit: int) -> list[str]:
    ids = list(dict.fromkeys(str(track["id"]) for track in manifest["tracks"]))
    if not ids:
        return []
    size = min(limit, len(ids))
    return [ids[i * len(ids) // size] for i in range(size)]


def _request_json(request: Request, *, timeout: int = 30) -> dict:
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} calling {request.full_url}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Cannot reach {request.full_url}: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected JSON response (expected object)")
    return result


def verify_sample(manifest: dict, *, audiomuse_url: str, plex_url: str,
                  token: str = "", cookie: str = "", sample_size: int = 12,
                  progress: Progress | None = None) -> dict:
    """Verify a bounded sample against actual Plex tracks; no full-library export."""
    if not 1 <= sample_size <= 25:
        raise ValueError("--verify-sample must be between 1 and 25")
    ids = _sample_ids(manifest, sample_size)
    if not ids:
        return {"sampled": 0, "matched": 0, "missing": [], "title_mismatches": [],
                "metadata_compared": 0, "verified": False,
                "message": "No analyzed AudioMuse tracks yet"}
    if progress:
        progress(f"Verifying {len(ids)} AudioMuse IDs via direct Plex lookups (no full export)")
    request = Request(
        plex_url.rstrip("/") + "/api/plex/verify-rating-keys",
        data=json.dumps({"rating_keys": ids}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    result = _request_json(request)
    checked = result.get("results")
    if not isinstance(checked, list) or len(checked) != len(ids):
        raise RuntimeError("Plex verification returned an unexpected number of rows")
    by_id = {str(entry["id"]): entry for entry in checked}
    if len(by_id) != len(ids) or set(by_id) != set(ids):
        raise RuntimeError("Plex verification result IDs do not match requested IDs")
    matched = [i for i in ids if by_id[i].get("status") == "matched"]
    missing = [{"id": i, "status": by_id[i].get("status")} for i in ids if i not in matched]
    differences = []
    compared = 0
    if matched:
        # Upstream /api/sync?ids=... returns metadata without big embeddings.
        endpoint = audiomuse_url.rstrip("/") + "/api/sync?" + urlencode({
            "ids": ",".join(matched), "include_embeddings": "false"
        })
        if progress:
            progress(f"Fetching AudioMuse titles for {len(matched)} matched IDs")
        details = _request_json(Request(endpoint, headers=auth_headers(token=token, cookie=cookie)))
        audio_tracks = details.get("tracks")
        if not isinstance(audio_tracks, list):
            raise RuntimeError("Unexpected AudioMuse metadata response")
        by_audio = {str(t.get("id")): t for t in audio_tracks if isinstance(t, dict)}
        for track_id in matched:
            audio = by_audio.get(track_id)
            if not audio:
                differences.append({"id": track_id, "reason": "AudioMuse metadata missing"})
                continue
            audio_title = audio.get("title")
            plex_title = by_id[track_id].get("title")
            if audio_title and plex_title:
                compared += 1
                if _normalise_title(audio_title) != _normalise_title(plex_title):
                    differences.append({"id": track_id, "audio_title": audio_title,
                                        "plex_title": plex_title})
    return {
        "sampled": len(ids),
        "matched": len(matched),
        "missing": missing,
        "metadata_compared": compared,
        "title_mismatches": differences[:15],
        "verified": len(matched) == len(ids) and compared == len(matched) and not differences,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://media-server:8098")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--plex-tsv", type=Path, help="Local Plex full-library TSV file")
    source.add_argument("--plex-url", default="http://127.0.0.1:5051",
                        help="Existing Plex Playlist Manager URL (default: local port 5051)")
    parser.add_argument("--all", action="store_true", help="Fetch all pages, not just the first")
    parser.add_argument("--manifest-only", action="store_true",
                        help="Skip potentially slow Plex export; verify AudioMuse alone")
    parser.add_argument("--verify-sample", type=int, nargs="?", const=12,
                        help="Verify 1-25 AudioMuse IDs directly against Plex (no full export)")
    args = parser.parse_args(argv)
    try:
        token = load_api_token()
        manifest = get_manifest(
            args.url,
            token=token,
            cookie=os.environ.get("AUDIOMUSE_COOKIE", ""),
            all_pages=args.all,
            progress=show_progress,
        )
        if args.verify_sample is not None:
            if args.plex_tsv:
                raise ValueError("--verify-sample cannot be combined with --plex-tsv")
            output = verify_sample(
                manifest,
                audiomuse_url=args.url,
                plex_url=args.plex_url,
                token=token,
                cookie=os.environ.get("AUDIOMUSE_COOKIE", ""),
                sample_size=args.verify_sample,
                progress=show_progress,
            )
            print(json.dumps(output, indent=2, ensure_ascii=False), flush=True)
            return 0 if output["verified"] else 2
        if args.manifest_only:
            show_progress("Skipping Plex ID comparison (--manifest-only)")
            plex = None
        elif args.plex_tsv:
            show_progress(f"Loading local Plex TSV: {args.plex_tsv}")
            plex = plex_ids(args.plex_tsv)
            show_progress(f"Loaded {len(plex)} Plex track IDs")
        else:
            plex = plex_ids_from_manager(args.plex_url, progress=show_progress)
        print(json.dumps(compare(manifest, plex), indent=2), flush=True)
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"AudioMuse probe: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
