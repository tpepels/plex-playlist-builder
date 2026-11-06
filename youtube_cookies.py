"""Native YouTube cookie import for the single-user downloader dashboard."""
from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path

from flask import abort, redirect, request, url_for

MAX_BYTES = 1024 * 1024
HEADERS = {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}
BUSY = {"queued", "running", "finalizing"}


class CookieFileError(ValueError):
    pass


def cookie_path(state_dir, configured=""):
    return Path(configured.strip()) if configured and configured.strip() else Path(state_dir) / "cookies.txt"


def filter_youtube_cookies(blob):
    if not blob or len(blob) > MAX_BYTES:
        raise CookieFileError("size")
    try:
        lines = blob.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        raise CookieFileError("format") from None
    if not lines or lines[0].strip() not in HEADERS:
        raise CookieFileError("format")
    retained = []
    for line in lines[1:]:
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise CookieFileError("format")
        domain_raw, include_sub, path, secure, expiry, name, value = fields
        domain = domain_raw.removeprefix("#HttpOnly_").lstrip(".").lower()
        if (include_sub not in {"TRUE", "FALSE"}
                or secure not in {"TRUE", "FALSE"}
                or not path.startswith("/") or not expiry.isdigit()
                or not name or any("\x00" in field for field in fields)):
            raise CookieFileError("format")
        if domain == "youtube.com" or domain.endswith(".youtube.com"):
            retained.append(line)
    if not retained:
        raise CookieFileError("missing")
    return "# Netscape HTTP Cookie File\n" + "\n".join(retained) + "\n"


def write_private(path, contents):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".youtube-cookies-", dir=destination.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def register(app, jobs, jobs_lock, destination):
    destination = Path(destination)
    token = secrets.token_urlsafe(32)

    @app.context_processor
    def cookie_context():
        result = request.args.get("cookies", "")
        return {
            "youtube_cookies_ready": destination.is_file(),
            "youtube_cookie_token": token,
            "youtube_cookie_notice": result if result in {
                "saved", "removed", "busy", "size", "format", "missing", "error"
            } else "",
        }

    def protect_form():
        if not secrets.compare_digest(request.form.get("_cookie_token", ""), token):
            abort(403)
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin != request.host_url.rstrip("/"):
            abort(403)

    def busy():
        with jobs_lock:
            return any(job.get("status") in BUSY or job.get("active_resolution_tracks")
                       for job in jobs.values())

    def finish(code):
        return redirect(url_for("index", cookies=code), code=303)

    @app.post("/youtube/cookies/upload")
    def upload_youtube_cookies():
        protect_form()
        if busy():
            return finish("busy")
        upload = request.files.get("cookies")
        if upload is None or not upload.filename:
            return finish("missing")
        try:
            cleaned = filter_youtube_cookies(upload.stream.read(MAX_BYTES + 1))
            write_private(destination, cleaned)
        except CookieFileError as exc:
            return finish(str(exc))
        except OSError:
            return finish("error")
        return finish("saved")

    @app.post("/youtube/cookies/remove")
    def remove_youtube_cookies():
        protect_form()
        if busy():
            return finish("busy")
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            return finish("error")
        return finish("removed")
