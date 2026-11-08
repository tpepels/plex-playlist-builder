"""Fixed-upstream proxy for the complete yt-dlp UI and APIs.

The downloader remains a separate container. The playlist manager is the
single browser entry point; all downloader features are served on the same
origin at /downloads/engine/. No arbitrary target URLs are accepted.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

PREFIX = "/downloads/engine"
MAX_REQUEST = 12 * 1024 * 1024
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
REQUEST_HEADERS = ("accept", "content-type", "origin", "range", "if-none-match",
                   "if-modified-since", "x-requested-with")
RESPONSE_HEADERS = ("content-type", "content-disposition", "cache-control",
                    "etag", "last-modified", "content-range", "accept-ranges",
                    "x-album-job-id")
router = APIRouter()


def downloader_origin():
    origin = os.getenv("YTDLP_SERVER_URL", "http://yt-dlp-server:4545").rstrip("/")
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment):
        raise RuntimeError("YTDLP_SERVER_URL must be an HTTP(S) origin only")
    return origin


def _check_same_origin(request):
    """Reject cross-origin browser mutations even though the engine is on LAN."""
    expected = urlsplit(str(request.base_url))
    for header in ("origin", "referer"):
        raw = request.headers.get(header)
        if not raw:
            continue
        actual = urlsplit(raw)
        if actual.scheme != expected.scheme or actual.netloc != expected.netloc:
            raise HTTPException(status_code=403, detail="Cross-origin downloader mutation blocked")


def _upstream_headers(request):
    result = {key: request.headers[key] for key in REQUEST_HEADERS
              if key in request.headers}
    # Force identity encoding; no Content-Encoding or Content-Length is copied.
    # The browser receives the streamed, decoded result.
    result["accept-encoding"] = "identity"
    result["x-forwarded-prefix"] = PREFIX
    result["x-forwarded-host"] = request.headers.get("host", "")
    result["x-forwarded-proto"] = request.url.scheme
    return result


def _rewrite_location(value, origin):
    """Never disclose internal Docker URLs in a Location header."""
    parsed = urlsplit(value)
    if not parsed.scheme and not parsed.netloc and value.startswith("/") and not value.startswith("//"):
        if value.startswith(PREFIX + "/"):
            return value
        return PREFIX + value
    if parsed.scheme and parsed.netloc and value.startswith(origin + "/"):
        return PREFIX + value[len(origin):]
    return value


def _outgoing_headers(response, origin):
    headers = {key: response.headers[key] for key in RESPONSE_HEADERS
               if key in response.headers}
    location = response.headers.get("location")
    if location:
        headers["location"] = _rewrite_location(location, origin)
    headers["cache-control"] = "no-store"
    headers["x-content-type-options"] = "nosniff"
    return headers


async def upstream_json(path, *, timeout=7):
    """Bounded internal read used by the progress and Plex sync integrations."""
    url = downloader_origin() + path
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers={"accept": "application/json"})
            response.raise_for_status()
            data = response.json()
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        raise RuntimeError("Download engine is unavailable; its jobs were not modified") from exc
    if not isinstance(data, (dict, list)):
        raise RuntimeError("Download engine returned an invalid status payload")
    return data


@router.api_route(PREFIX, methods=METHODS, include_in_schema=False)
async def downloader_without_slash(request: Request):
    query = ("?" + request.url.query) if request.url.query else ""
    return RedirectResponse(PREFIX + "/" + query, status_code=307)


@router.api_route(PREFIX + "/{path:path}", methods=METHODS, include_in_schema=False)
async def proxy_downloader(request: Request, path: str):
    if request.method not in {"GET", "HEAD"}:
        _check_same_origin(request)
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_REQUEST:
                raise HTTPException(status_code=413, detail="Downloader request too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid request length") from None
    # The upstream has no user-selectable target. Never forward auth tokens,
    # Host, X-Forwarded-For, or browser cookies to yt-dlp.
    origin = downloader_origin()
    target = origin + "/" + path.lstrip("/")
    if request.url.query:
        target += "?" + request.url.query

    body = b""
    if request.method not in {"GET", "HEAD"}:
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_REQUEST:
                raise HTTPException(status_code=413, detail="Downloader request too large")
            chunks.append(chunk)
        body = b"".join(chunks)

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5, read=260, write=60, pool=5),
        follow_redirects=False,
    )
    try:
        req = client.build_request(
            request.method, target, headers=_upstream_headers(request),
            content=body,
        )
        remote = await client.send(req, stream=True)
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        await client.aclose()
        return JSONResponse(
            {"error": "Download engine unreachable", "next_action":
             "Check the yt-dlp-server container and retry; no request was confirmed."},
            status_code=502, headers={"Cache-Control": "no-store"},
        )
    except Exception:
        await client.aclose()
        raise

    async def chunks():
        try:
            async for chunk in remote.aiter_bytes():
                yield chunk
        finally:
            await remote.aclose()
            await client.aclose()

    return StreamingResponse(
        chunks(), status_code=remote.status_code,
        headers=_outgoing_headers(remote, origin),
    )


@router.get("/api/downloads/bridge/status")
async def bridge_status():
    try:
        data = await upstream_json("/api/jobs")
    except RuntimeError as exc:
        return JSONResponse(
            {"ok": False, "engine": "offline", "error": str(exc)},
            status_code=503, headers={"Cache-Control": "no-store"},
        )
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return JSONResponse({"ok": False, "engine": "invalid",
                             "error": "Download engine returned invalid jobs"},
                            status_code=502)
    counts = {"queued": 0, "running": 0, "finalizing": 0,
              "needs_metadata": 0, "succeeded": 0, "partial": 0, "failed": 0}
    for job in jobs:
        if isinstance(job, dict):
            state = job.get("status")
            if state in counts:
                counts[state] += 1
    return JSONResponse(
        {"ok": True, "engine": "online", "counts": counts,
         "total": len(jobs)}, headers={"Cache-Control": "no-store"},
    )
