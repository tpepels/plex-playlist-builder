"""Read-only discovery of albums curated by In Sheep's Clothing Hi-Fi.

A bounded, cached crawl of its public *album collection* pages. Site records
remain attributed to their source; classification is local, advisory and based
only on explicit collection styles / tags, never fabricated genres.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit
import json
import os
import re
import threading
import time

import requests
from bs4 import BeautifulSoup

from .insights import normalize
from .youtube_album import base_album_title, _ensemble_credit_matches

SOURCE = "https://insheepsclothinghifi.com"
SOURCE_HOSTS = frozenset({"insheepsclothinghifi.com", "www.insheepsclothinghifi.com"})
SOURCE_STYLES = ("japan", "jazz", "funk", "fusion", "ambient", "experimental", "pop", "electronic")
DEFAULT_TTL = 24 * 60 * 60
MAX_ALBUMS = 220
DETAIL_LIMIT = 36
RECOMMENDED_DETAIL_LIMIT = 12
CACHE = Path(os.environ.get("ISC_DISCOVERY_CACHE", "/data/isc-discovery.json"))
_LOCK = threading.Lock()
LINK_RE = re.compile(r"/album/[^/?#]+/?$")
DASH = re.compile(r"\s+[–—]\s+")


def _safe_album_url(value):
    """Only source site links under /album/, never external SSRF or script URLs."""
    if not isinstance(value, str):
        return ""
    url = urljoin(SOURCE + "/", value)
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in SOURCE_HOSTS
            or parts.username is not None or parts.password is not None
            or parts.port is not None or not LINK_RE.fullmatch(parts.path)):
        return ""
    return urlunsplit(("https", parts.hostname, parts.path, "", ""))


def _parse_credit(label):
    """Collection cards use the format 'Artist – Album'. Be conservative."""
    parts = DASH.split(label.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    artist, album = (x.strip() for x in parts)
    if not artist or not album or len(artist) > 160 or len(album) > 230:
        return None
    return artist, album


def parse_collection(html, *, style):
    """Select actual collection album links, not navigation/article links."""
    soup = BeautifulSoup(html, "html.parser")
    records = {}
    for a in soup.select('a[href*="/album/"]'):
        url = _safe_album_url(a.get("href"))
        if not url:
            continue
        # Card labels may contain artwork with alt text, nested heading, etc.
        label = a.get_text(" ", strip=True)
        if not _parse_credit(label):
            image = a.find("img")
            label = image.get("alt", "").removesuffix(" album cover").strip() if image else ""
        credit = _parse_credit(label)
        if not credit:
            continue
        artist, album = credit
        records.setdefault(url, {
            "artist": artist, "album": album,
            "source_url": url, "source_styles": [style],
            "details_status": "listing",
            "site_styles": [], "site_tags": [], "year": None,
        })
    return list(records.values())


def parse_album_details(html):
    """Read only labeled style/tag/year fields of an album detail page."""
    soup = BeautifulSoup(html, "html.parser")
    # Label/value are rendered as ordinary text and links by the source CMS.
    # Restrict to the short heading's immediate text section so credits,
    # suggested albums and long review prose do not become false genres.
    labels = {}
    for node in soup.select("p, div, li"):
        content = node.get_text(" ", strip=True)
        if not content or len(content) > 240:
            continue
        for field in ("Style", "Tags", "Released"):
            if content.startswith(field + ":") and field not in labels:
                labels[field] = content.split(":", 1)[1].strip()
    # A fallback for uncomplicated markup with inline labels.
    text_lines = [x.strip() for x in soup.get_text("\n").splitlines() if x.strip()]
    for label in ("Style", "Tags", "Released"):
        if label in labels:
            continue
        try:
            pos = text_lines.index(label + ":")
        except ValueError:
            continue
        if pos + 1 < len(text_lines):
            labels[label] = " ".join(text_lines[pos + 1:pos + 5])[:180].split("Label:")[0].strip()
    def terms(s):
        return [v.strip() for v in re.split(r"\s*[/,]\s*", s) if v.strip() and len(v.strip()) < 70][:12]
    year = re.search(r"\b(?:19|20)\d\d\b", labels.get("Released", ""))
    suggestions = []
    # An album's "Suggested Records" section contains directly linked curated
    # recommendations. Do not parse arbitrary artists in editorial prose.
    section = soup.find(
        lambda node: node.name in ("h2", "h3", "h4", "strong")
        and node.get_text(" ", strip=True).casefold() == "suggested records"
    )
    if section:
        for a in section.find_all_next("a", href=True, limit=32):
            url = _safe_album_url(a.get("href"))
            image = a.find("img")
            label = a.get_text(" ", strip=True)
            credit = _parse_credit(label)
            if not credit and image:
                credit = _parse_credit(image.get("alt", "").removesuffix(" album cover"))
            if not url or not credit or any(x["source_url"] == url for x in suggestions):
                continue
            suggestions.append({
                "source_url": url, "artist": credit[0], "album": credit[1],
            })
            if len(suggestions) == 4:
                break
    return {
        "site_styles": terms(labels.get("Style", "")),
        "site_tags": terms(labels.get("Tags", "")),
        "year": int(year.group()) if year else None,
        "suggestions": suggestions,
    }


def categorize(record):
    """Optional recommendation moods; not claimed to be ISC's official taxonomy."""
    tokens = {normalize(x) for x in record.get("source_styles", []) +
              record.get("site_styles", []) + record.get("site_tags", [])}
    def has(*phrases):
        return any(normalize(p) in x for x in tokens for p in phrases)
    categories = []
    japan = has("japan", "japanese")
    jazz = has("jazz", "fusion")
    funk = has("funk", "jazz funk", "soul funk", "boogie")
    ambient = has("ambient", "new age", "drone", "environmental music")
    experimental = has("experimental", "avant", "art pop", "indietronica", "minimal")
    pop = has("pop", "rock pop folk", "synth pop")
    if japan and jazz and funk:
        categories.append("japanese-jazz-funk")
    if has("deep listening") or ambient or has("modern classical", "minimalism"):
        categories.append("deep-listening")
    if jazz and (ambient or has("chamber jazz", "ecm", "modern classical", "minimal jazz")):
        categories.append("ecm-jazz")
    if ambient:
        categories.append("ambient")
    if (pop and experimental) or has("indietronica", "avant pop", "art pop", "experimental pop"):
        categories.append("experimental-pop")
    return categories


def _fetch_html(url):
    # Only allowlisted paths from our own generated URLs are fetched.
    parts = urlsplit(url)
    if parts.hostname not in SOURCE_HOSTS or parts.scheme != "https":
        raise ValueError("Only In Sheep's Clothing pages can be fetched")
    response = requests.get(
        url, timeout=(5, 12), allow_redirects=False,
        headers={"User-Agent": "PlexPlaylistBuilder/1.0 (personal music discovery; modest cached requests)",
                 "Accept": "text/html"},
    )
    if response.status_code in (301, 302, 303, 307, 308):
        # Only same-origin redirects; no automatic external redirects.
        dest = urljoin(url, response.headers.get("Location", ""))
        target = urlsplit(dest)
        if target.scheme != "https" or target.hostname not in SOURCE_HOSTS:
            raise ValueError("Unexpected cross-domain redirect from source")
        response = requests.get(
            dest, timeout=(5, 12), allow_redirects=False,
            headers={"User-Agent": "PlexPlaylistBuilder/1.0 (personal music discovery)"}
        )
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise ValueError("Source HTML exceeds safe size limit")
    if "html" not in response.headers.get("Content-Type", "text/html").lower():
        raise ValueError("Source returned a non-HTML response")
    return response.text


def _read_cache():
    try:
        payload = json.loads(CACHE.read_text(encoding="utf-8"))
        if (payload.get("schema") == 1 and isinstance(payload.get("albums"), list)
                and isinstance(payload.get("fetched_at"), (int, float))):
            return payload
    except (FileNotFoundError, ValueError, OSError, TypeError):
        pass
    return None


def _write_cache(payload):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        path = CACHE.with_suffix(".tmp")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        path.replace(CACHE)
    except OSError:
        # A read-only cache directory must not block a successful scan.
        pass


def fetch_catalog(*, refresh=False, fetch_html=None):
    """Fetch a bounded cross-section of the source collection, with stale fallback.

    There is no general internet crawling: only 7 known style collections and
    at most 36 linked album pages. Refresh is user-initiated; ordinary requests
    reuse a day-old persistent cache.
    """
    fetcher = fetch_html or _fetch_html
    with _LOCK:
        cached = _read_cache()
        if cached and not refresh and time.time() - cached["fetched_at"] < DEFAULT_TTL:
            return {**cached, "cached": True, "stale": False, "warnings": []}

        by_url = {}
        warnings = []
        sources_succeeded = 0
        for style in SOURCE_STYLES:
            url = SOURCE + "/collection/?style=" + quote(style)
            try:
                albums = parse_collection(fetcher(url), style=style)
                if not albums:
                    warnings.append(f"{style}: source returned no recognizable album links")
                    continue
                sources_succeeded += 1
                for album in albums:
                    source_url = album["source_url"]
                    if source_url in by_url:
                        existing = by_url[source_url]["source_styles"]
                        if style not in existing:
                            existing.append(style)
                    elif len(by_url) < MAX_ALBUMS:
                        by_url[source_url] = album
            except (requests.RequestException, ValueError, OSError) as exc:
                warnings.append(f"{style}: source unavailable ({type(exc).__name__})")
        if not sources_succeeded:
            if cached:
                return {**cached, "cached": True, "stale": True,
                        "warnings": ["Using previous catalog: source unavailable"] + warnings}
            raise RuntimeError("In Sheep's Clothing collection is unavailable. " +
                               "; ".join(warnings[:3]))
        records = list(by_url.values())
        # Enrich a limited sample with explicit site tags / styles to support
        # nuanced moods; other records retain reliable category-level labels.
        # Bound remote traffic and keep discovery responsive while enriching
        # records with site-defined tags. Only 3 album pages in flight.
        def enrich(album):
            try:
                return parse_album_details(fetcher(album["source_url"]))
            except (requests.RequestException, ValueError, OSError):
                return None

        with ThreadPoolExecutor(max_workers=3) as workers:
            details = workers.map(enrich, records[:DETAIL_LIMIT])
            for album, detail in zip(records[:DETAIL_LIMIT], details):
                if detail:
                    album.update(detail)
                    album["details_status"] = "detail"
        # Album detail pages also publish a small "Suggested Records" set.
        # Include these actual album links as distinct discoveries, but don't
        # assign the parent's genres to them; that would be misleading.
        extras = []
        for parent in records[:DETAIL_LIMIT]:
            suggestions = parent.pop("suggestions", [])
            for suggestion in suggestions:
                url = suggestion["source_url"]
                origin = {
                    "artist": parent["artist"], "album": parent["album"],
                    "source_url": parent["source_url"],
                }
                if url in by_url:
                    existing = by_url[url].setdefault("recommended_by", [])
                    if origin not in existing:
                        existing.append(origin)
                elif len(records) + len(extras) < MAX_ALBUMS:
                    record = {
                        **suggestion, "source_styles": [], "site_styles": [],
                        "site_tags": [], "details_status": "recommendation",
                        "year": None, "recommended_by": [origin],
                    }
                    by_url[url] = record
                    extras.append(record)
        with ThreadPoolExecutor(max_workers=3) as workers:
            details = workers.map(enrich, extras[:RECOMMENDED_DETAIL_LIMIT])
            for album, detail in zip(extras[:RECOMMENDED_DETAIL_LIMIT], details):
                if detail:
                    album.update(detail)
                    album.pop("suggestions", None)
                    album["details_status"] = "detail"
        records.extend(extras)
        for album in records:
            album["moods"] = categorize(album)
        payload = {
            "schema": 1, "fetched_at": time.time(),
            "albums": records, "sources_scanned": sources_succeeded,
            "source_name": "In Sheep's Clothing Hi-Fi",
        }
        _write_cache(payload)
        return {**payload, "cached": False, "stale": False, "warnings": warnings}


def _same_artist(a, b):
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return True
    # Accept the same named collaborators with a different delimiter, but
    # never a single artist as a match for a collaboration with other people.
    def collaborators(credit):
        pieces = [normalize(x) for x in re.split(
            r"\s*(?:,|&|\band\b)\s*", credit, flags=re.IGNORECASE)]
        return pieces if len(pieces) >= 2 and all(pieces) else []
    left, right = collaborators(a), collaborators(b)
    if left and right and left == right:
        return True
    # A different artist's album must never be called owned from a shared title.
    return _ensemble_credit_matches(a, b) or _ensemble_credit_matches(b, a)


def compare_library(catalog, tracks):
    """Annotate source albums as in library / edition only / missing.

    Exact artist matching and a recognized release suffix are required. Do
    not use substring or song-title similarity to claim ownership.
    """
    albums = defaultdict(dict)
    for track in tracks:
        artist = str(getattr(track, "grandparentTitle", "") or "").strip()
        title = str(getattr(track, "parentTitle", "") or "").strip()
        key = str(getattr(track, "ratingKey", "") or "").strip()
        if not artist or not title or not key:
            continue
        album_key = str(getattr(track, "parentRatingKey", "") or "") or f"{normalize(artist)}|{normalize(title)}"
        identity = (normalize(artist), normalize(base_album_title(title)), album_key)
        albums[identity].setdefault(key, (artist, title, album_key))

    available = []
    for (_artist, _title, album_key), entries in albums.items():
        sample = next(iter(entries.values()))
        available.append({
            "artist": sample[0], "album": sample[1], "album_rating_key": album_key,
            "tracks": len(entries),
        })
    index = defaultdict(list)
    for item in available:
        index[normalize(base_album_title(item["album"]))].append(item)

    matches = []
    for record in catalog["albums"]:
        artist, album = record["artist"], record["album"]
        versions = [x for x in index[normalize(base_album_title(album))]
                    if _same_artist(artist, x["artist"])]
        exact = [x for x in versions if normalize(x["album"]) == normalize(album)]
        status = "owned" if exact else "edition" if versions else "missing"
        matches.append({
            **record, "status": status,
            "plex_matches": exact if exact else versions,
        })
    summary = {
        "total": len(matches),
        "owned": sum(x["status"] == "owned" for x in matches),
        "edition": sum(x["status"] == "edition" for x in matches),
        "missing": sum(x["status"] == "missing" for x in matches),
        "source_categories": sorted({m for x in matches for m in x["moods"]}),
    }
    return {
        "source": catalog.get("source_name", "In Sheep's Clothing Hi-Fi"),
        "source_url": SOURCE + "/collection/",
        "fetched_at": datetime.fromtimestamp(catalog["fetched_at"], tz=timezone.utc).isoformat(),
        "cached": catalog.get("cached", False),
        "stale": catalog.get("stale", False),
        "warnings": catalog.get("warnings", []),
        "sources_scanned": catalog.get("sources_scanned", 0),
        "summary": summary, "albums": matches,
    }
