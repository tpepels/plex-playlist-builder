"""Loopback API between the unprivileged web and root-owned VPN controller.

They share one Docker network namespace, but only the gateway process has
NET_ADMIN privileges. VPN keys stay in root-owned profile files.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Blueprint, jsonify, request

GATEWAY = "http://127.0.0.1:8787"
TEST_LOCK = threading.Lock()
test_active = False
_PRIVATE_KEY_DIRECTIVE = re.compile(r"(?m)^([ \t]*PrivateKey[ \t]*=[ \t]*)([^\r\n]*)(\r?)$")
_SURFSHARK_PLACEHOLDER = "<insert_your_private_key_here>"


class MissingClientKey(ValueError):
    """A Surfshark location template needs the registered client private key."""


def complete_wireguard_template(content, private_key=""):
    """Fill only Surfshark's client-key placeholder, never overwrite real keys.

    Neither the supplied key nor the resulting configuration is logged or
    returned in a response. The gateway already saves sanitized configs
    under owner-only permissions.
    """
    matches = list(_PRIVATE_KEY_DIRECTIVE.finditer(content))
    if len(matches) != 1:
        raise ValueError("WireGuard config must contain one PrivateKey directive")
    match = matches[0]
    current = match.group(2).strip()
    if current != _SURFSHARK_PLACEHOLDER:
        return content
    supplied = private_key.strip()
    if not supplied:
        raise MissingClientKey(
            "These Surfshark files are templates without your client private key. "
            "Generate a WireGuard key pair in Surfshark → VPN → Manual setup → "
            "WireGuard, then paste its private key once below for all selected files."
        )
    try:
        decoded = base64.b64decode(supplied, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError(
            "The supplied WireGuard private key is invalid. Copy the complete "
            "private key generated in your Surfshark account."
        ) from None
    if len(decoded) != 32:
        raise ValueError(
            "The supplied WireGuard private key is invalid. A WireGuard key "
            "must decode to exactly 32 bytes."
        )
    return content[:match.start(2)] + supplied + content[match.end(2):]


def configured():
    return os.getenv("VPN_GATEWAY_ENABLED") == "1"


def control_token():
    # Read from the existing state volume. The gateway generates this
    # credential on its own; it is NEVER exposed to the browser.
    path = Path(os.getenv("VPN_CONTROL_TOKEN_FILE", "/data/state/.vpn-control-token"))
    try:
        token = path.read_text().strip()
        if len(token) < 32:
            raise ValueError("invalid token")
        return token
    except (OSError, ValueError):
        raise RuntimeError("VPN service is starting or unavailable. Rebuild the downloader once, then refresh.")


def gateway_call(path, payload=None):
    if path not in {"/profiles", "/rotate", "/test", "/direct"} and not (
        path.startswith("/activate/") or path.startswith("/delete/")
    ):
        raise ValueError("Unsupported VPN gateway operation")
    try:
        token = control_token()
    except RuntimeError as exc:
        return {"error": str(exc)}, 503
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(GATEWAY + path, data=data,
                  headers={"X-VPN-Key": token,
                           "Content-Type": "application/json"},
                  method="POST" if data is not None else "GET")
    try:
        with urlopen(req, timeout=(8 if path == "/profiles" else 240 if path == "/rotate" else 70)) as response:
            return json.load(response), response.status
    except HTTPError as exc:
        try:
            result = json.load(exc)
        except (ValueError, OSError):
            result = {"error": "VPN gateway rejected the operation"}
        return result, exc.code
    except (URLError, TimeoutError, OSError):
        return {"error": "VPN gateway unavailable; check container and VPN mode"}, 503


def idle(jobs, jobs_lock):
    from surfshark_vpn import busy_jobs
    with jobs_lock:
        return not busy_jobs(list(jobs.values()))


def register(app, jobs, jobs_lock):
    bp = Blueprint("vpn_ui", __name__)

    @app.before_request
    def guard_downloads_during_test():
        if not configured() or request.method != "POST":
            return None
        path = request.path
        is_job_change = (path in {"/download", "/api/albums",
                                  "/api/probe", "/api/fallback-track"}
                         or path == "/api/jobs/retry-failed"
                         or (path.startswith("/api/jobs/") and
                             (path.endswith("/retry") or path.endswith("/resolve"))))
        if not is_job_change:
            return None
        global test_active
        with TEST_LOCK:
            running = test_active
        if running:
            data, code = gateway_call("/profiles")
            if code == 200 and data.get("test", {}).get("done", True):
                with TEST_LOCK:
                    test_active = False
                return None
            return jsonify(error="VPN rotation test in progress; downloads are temporarily blocked"), 409

    @bp.before_request
    def safety():
        if not configured():
            return jsonify(enabled=False,
                           error="VPN service unavailable in this deployment. Update and rebuild the downloader once."), 503
        if request.method == "POST":
            # A required custom header blocks cross-origin HTML form submits.
            # No admin secret is requested from the user or sent to the browser.
            if request.headers.get("X-Requested-With") != "XMLHttpRequest":
                return jsonify(error="VPN action must originate from this dashboard"), 403
            origin = request.headers.get("Origin", "").rstrip("/")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="Cross-origin VPN actions are prohibited"), 403

    @bp.get("/api/vpn/profiles")
    def profiles():
        return gateway_call("/profiles")

    @bp.post("/api/vpn/profiles")
    def upload():
        if not idle(jobs, jobs_lock):
            return jsonify(error="Cannot change VPN profiles with active or queued downloads"), 409
        files = request.files.getlist("configs")
        if not files or len(files) > 20:
            return jsonify(error="Select between 1 and 20 WireGuard .conf files"), 400

        # Preflight *every* selected file before saving any profile. A missing
        # shared private key must not leave a confusing partial batch behind.
        prepared = []
        private_key = request.form.get("private_key", "")
        for f in files:
            payload = f.read(8193)
            if len(payload) > 8192:
                return jsonify(error=f"{f.filename}: file exceeds 8 KB"), 400
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError:
                return jsonify(error=f"{f.filename}: configuration must be UTF-8 text"), 400
            try:
                completed = complete_wireguard_template(content, private_key)
            except MissingClientKey as exc:
                return jsonify(error=str(exc), needs_private_key=True), 422
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            prepared.append((f.filename, f.filename.removesuffix(".conf"), completed))

        results = []
        for filename, label, content in prepared:
            item, status = gateway_call("/profiles", {
                "filename": filename, "country": label, "content": content})
            if status != 200:
                return jsonify(error=item.get("error", "Upload failed"),
                               uploaded=results), status
            results.append({key: item[key] for key in ("id", "filename", "country")})
        # Uploading profiles must never change the normal direct connection.
        # They are held ready for explicit manual testing or failure recovery.
        return jsonify(ok=True, uploaded=results,
                       connected=None, warning=None, default_mode="direct")

    @bp.post("/api/vpn/activate/<profile_id>")
    def activate(profile_id):
        if not idle(jobs, jobs_lock):
            return jsonify(error="Wait until all downloads and retries are idle"), 409
        return gateway_call("/activate/" + profile_id, {})

    @bp.post("/api/vpn/delete/<profile_id>")
    def delete(profile_id):
        if not idle(jobs, jobs_lock):
            return jsonify(error="Wait until all downloads are idle"), 409
        return gateway_call("/delete/" + profile_id, {})

    @bp.post("/api/vpn/rotate")
    def rotate():
        if not idle(jobs, jobs_lock):
            return jsonify(error="Wait until all downloads and retries are idle"), 409
        return gateway_call("/rotate", {})

    @bp.post("/api/vpn/direct")
    def direct():
        if not idle(jobs, jobs_lock):
            return jsonify(error="Wait for active downloads to finish"), 409
        return gateway_call("/direct", {})

    @bp.post("/api/vpn/test")
    def test():
        global test_active
        if not idle(jobs, jobs_lock):
            return jsonify(error="Wait until all downloads and retries are idle"), 409
        with TEST_LOCK:
            if test_active:
                return jsonify(error="VPN rotation test already running"), 409
            test_active = True
        data, code = gateway_call("/test", {})
        if code != 200:
            with TEST_LOCK:
                test_active = False
        return data, code

    app.register_blueprint(bp)
