"""Native YouTube cookie import for the single-user downloader dashboard."""
from __future__ import annotations

import http.cookiejar
import os
import re
import secrets
import stat
import subprocess
import tempfile
import threading
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from pathlib import Path

from flask import abort, jsonify, redirect, request, url_for

MAX_BYTES = 1024 * 1024
HEADERS = {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}
BUSY = {"queued", "running", "finalizing"}
CHECK_VIDEO_ID = "jNQXAC9IVRw"
CHECK_VIDEO_URL = f"https://www.youtube.com/watch?v={CHECK_VIDEO_ID}"
LOGIN_PAGE_URL = "https://www.youtube.com/feed/you"
LOGIN_PATTERN = re.compile(r'"LOGGED_IN"\s*:\s*(true|false)')


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




def _verification_result(status, message):
    """Three UI states, with a fixed diagnostic message and no secrets."""
    return {"status": status, "message": message}


def youtube_login_state(cookie_file):
    """Confirm account recognition from YouTube's own page, not cookie names.

    A successful public-video extraction alone cannot prove that cookies
    represent a signed-in YouTube account. The configured VPN network
    namespace is used for this request as well.
    """
    jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
    jar.load(ignore_discard=True, ignore_expires=True)
    for cookie in jar:
        # Netscape exports often encode a session cookie with expiry 0.
        if cookie.expires == 0:
            cookie.expires = None
            cookie.discard = True
    opener = build_opener(HTTPCookieProcessor(jar))
    req = Request(LOGIN_PAGE_URL, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.8",
        "Accept": "text/html",
    })
    with opener.open(req, timeout=12) as response:
        url = response.geturl()
        if url.startswith(("https://accounts.google.com/", "https://accounts.youtube.com/")):
            return False
        if not url.startswith(("https://www.youtube.com/", "https://youtube.com/")):
            return None
        body = response.read(2_000_000).decode("utf-8", errors="replace")
    states = set(LOGIN_PATTERN.findall(body))
    if states == {"true"}:
        return True
    if states == {"false"}:
        return False
    return None


def verify_youtube_cookies(destination, extractor_args=None):
    """Verify online login AND metadata-only yt-dlp extraction.

    Return fixed, non-sensitive states. Never return raw subprocess or HTTP
    output, which could reveal session cookies or personal account details.
    A private temp copy prevents yt-dlp from modifying the installed file.
    """
    source = Path(destination)
    if not source.is_file():
        return _verification_result("rejected", "No YouTube cookie file is installed.")
    try:
        data = source.read_bytes()
        cleaned = filter_youtube_cookies(data)
    except (OSError, CookieFileError):
        return _verification_result(
            "rejected", "The saved cookie file is unreadable or is not a valid YouTube cookies.txt export."
        )

    fd, temporary = tempfile.mkstemp(prefix=".youtube-verify-", dir=source.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(cleaned)
        try:
            login = youtube_login_state(temporary)
        except (OSError, ValueError, HTTPError, URLError, http.cookiejar.LoadError):
            return _verification_result(
                "inconclusive",
                "YouTube's account page could not be checked. VPN, network or YouTube access may be blocking verification."
            )
        if login is False:
            return _verification_result(
                "rejected",
                "YouTube does not recognize a signed-in account with these cookies. Export a fresh YouTube session."
            )
        if login is None:
            return _verification_result(
                "inconclusive",
                "YouTube did not return a clear login status. The VPN or a YouTube challenge may be interfering."
            )
        cmd = [
            "yt-dlp", "--ignore-config", "--no-playlist",
            "--simulate", "--skip-download", "--no-cache-dir",
            "--no-warnings", "--no-progress",
            "--retries", "0", "--extractor-retries", "0",
            "--socket-timeout", "8",
            "--cookies", temporary,
        ]
        if extractor_args:
            cmd.extend(extractor_args(CHECK_VIDEO_URL))
        cmd.extend(["--print", "%(id)s", CHECK_VIDEO_URL])
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return _verification_result(
                "inconclusive",
                "YouTube recognized your session, but the yt-dlp metadata check timed out or could not start."
            )
        if completed.returncode == 0 and CHECK_VIDEO_ID in completed.stdout.splitlines():
            return _verification_result(
                "valid",
                "YouTube recognizes your signed-in session, and yt-dlp can extract video metadata with the saved cookies."
            )
        output = (completed.stdout + "\n" + completed.stderr).lower()
        if ("not a bot" in output or "http error 403" in output
                or "http 403" in output):
            return _verification_result(
                "inconclusive",
                "YouTube recognizes the account, but the current VPN/IP is blocked during yt-dlp extraction."
            )
        if "sign in" in output or "cookies are no longer valid" in output:
            return _verification_result(
                "rejected",
                "yt-dlp reports that YouTube requires sign-in again. Re-export the YouTube cookies."
            )
        return _verification_result(
            "inconclusive",
            "YouTube recognizes the account, but yt-dlp could not extract test-video metadata."
        )
    finally:
        os.unlink(temporary)


def register(app, jobs, jobs_lock, destination, extractor_args=None):
    destination = Path(destination)
    token = secrets.token_urlsafe(32)
    verify_lock = threading.Lock()

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
        if not verify_lock.acquire(blocking=False):
            return finish("busy")
        try:
            cleaned = filter_youtube_cookies(upload.stream.read(MAX_BYTES + 1))
            write_private(destination, cleaned)
        except CookieFileError as exc:
            return finish(str(exc))
        except OSError:
            return finish("error")
        finally:
            verify_lock.release()
        return finish("saved")

    @app.post("/youtube/cookies/remove")
    def remove_youtube_cookies():
        protect_form()
        if busy():
            return finish("busy")
        if not verify_lock.acquire(blocking=False):
            return finish("busy")
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            return finish("error")
        finally:
            verify_lock.release()
        return finish("removed")

    @app.post("/youtube/cookies/verify")
    def verify_saved_youtube_cookies():
        protect_form()
        if busy():
            return jsonify(_verification_result(
                "inconclusive", "Wait for running and queued downloads to finish before verifying cookies."
            )), 409
        if not verify_lock.acquire(blocking=False):
            return jsonify(_verification_result(
                "inconclusive", "A cookie update or verification is already running."
            )), 409
        try:
            return jsonify(verify_youtube_cookies(destination, extractor_args))
        finally:
            verify_lock.release()
