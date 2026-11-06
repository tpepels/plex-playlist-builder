"""Private YouTube cookie import and download-network preference management.

Only Netscape-format YouTube cookies are persisted; raw cookie values are
never returned to the browser, dashboard, job log or VPN gateway.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from surfshark_vpn import busy_jobs

MAX_COOKIES_SIZE = 1024 * 1024
COOKIE_HEADERS = frozenset({"# Netscape HTTP Cookie File", "# HTTP Cookie File"})


def cookie_path(state_dir):
    return Path(os.getenv("COOKIES_FILE") or Path(state_dir) / "cookies.txt")


def preferences_path(state_dir):
    return Path(state_dir) / "youtube-preferences.json"


def direct_fallback_enabled(state_dir):
    """Default-on, with an explicit persisted user-controlled opt-out."""
    try:
        obj = json.loads(preferences_path(state_dir).read_text(encoding="utf-8"))
        return obj.get("direct_fallback_enabled") is not False
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def parse_cookies(content):
    """Reject malformed files and persist only youtube.com cookie records.

    A browser may export cookies from all websites; those records must never
    be stored on this downloader. Netscape HttpOnly records are preserved.
    """
    if len(content) > MAX_COOKIES_SIZE or not content:
        raise ValueError("Cookie file must be nonempty and at most 1 MB")
    try:
        raw = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Cookie file must be UTF-8 Netscape cookies.txt") from None
    lines = raw.splitlines()
    if not lines or lines[0].strip() not in COOKIE_HEADERS:
        raise ValueError("Export a Netscape cookies.txt file with its required header")
    selected = []
    ignored = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        fields = line.split("\t")
        if len(fields) != 7 or any("\x00" in field for field in fields):
            raise ValueError("Cookie file contains malformed Netscape records")
        domain = fields[0].removeprefix("#HttpOnly_").lstrip(".").lower()
        if not domain or fields[1] not in {"TRUE", "FALSE"} or fields[3] not in {"TRUE", "FALSE"}:
            raise ValueError("Cookie file contains invalid domain or flags")
        if not fields[2].startswith("/") or not fields[4].isdigit() or not fields[5]:
            raise ValueError("Cookie file contains invalid paths or expiry data")
        if domain != "youtube.com" and not domain.endswith(".youtube.com"):
            ignored += 1
            continue
        selected.append(line)
    if not selected:
        raise ValueError("No YouTube cookies found. Export cookies for youtube.com")
    return "# Netscape HTTP Cookie File\n" + "\n".join(selected) + "\n", len(selected), ignored


def atomic_private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".youtube-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(value)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def register(app, jobs, jobs_lock, state_dir):
    bp = Blueprint("youtube_auth", __name__)
    cookies = cookie_path(state_dir)

    def authorize():
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return jsonify(error="Action must originate from the dashboard"), 403
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="Cross-origin actions are prohibited"), 403
        return None

    def idle():
        with jobs_lock:
            return not busy_jobs(list(jobs.values()))

    def status():
        try:
            info = cookies.stat()
            installed = info.st_size > 0 and cookies.is_file()
            modified = int(info.st_mtime) if installed else None
        except OSError:
            installed, modified = False, None
        return {
            "cookies_present": installed,
            "updated_at": modified,
            "direct_fallback_enabled": direct_fallback_enabled(state_dir),
        }

    @bp.get("/api/youtube/auth")
    def get_auth():
        return jsonify(status())

    @bp.post("/api/youtube/cookies")
    def upload_cookies():
        rejected = authorize()
        if rejected is not None:
            return rejected
        if not idle():
            return jsonify(error="Wait until all queued and running downloads finish"), 409
        uploaded = request.files.get("cookies")
        if not uploaded:
            return jsonify(error="Choose a Netscape-format cookies.txt file"), 400
        content = uploaded.stream.read(MAX_COOKIES_SIZE + 1)
        try:
            filtered, count, ignored = parse_cookies(content)
            atomic_private_write(cookies, filtered)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except OSError:
            return jsonify(error="Could not store the cookie file privately"), 503
        return jsonify(ok=True, count=count, ignored=ignored, **status())

    @bp.delete("/api/youtube/cookies")
    def delete_cookies():
        rejected = authorize()
        if rejected is not None:
            return rejected
        if not idle():
            return jsonify(error="Wait until all queued and running downloads finish"), 409
        try:
            cookies.unlink(missing_ok=True)
        except OSError:
            return jsonify(error="Could not remove the cookie file"), 503
        return jsonify(ok=True, **status())

    @bp.post("/api/youtube/auth")
    def update_auth():
        rejected = authorize()
        if rejected is not None:
            return rejected
        data = request.get_json(silent=True)
        if (not isinstance(data, dict)
                or type(data.get("direct_fallback_enabled")) is not bool):
            return jsonify(error="Expected direct_fallback_enabled as a boolean"), 400
        try:
            atomic_private_write(preferences_path(state_dir), json.dumps({
                "direct_fallback_enabled": data["direct_fallback_enabled"],
                "updated_at": int(time.time()),
            }))
        except OSError:
            return jsonify(error="Could not save the recovery preference"), 503
        return jsonify(ok=True, **status())

    app.register_blueprint(bp)
