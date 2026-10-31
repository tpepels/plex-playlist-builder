"""Administrator-only API bridging the unprivileged UI to the VPN gateway.

No uploaded WireGuard secret is persisted in downloader state or job logs.
Only the isolated gateway can modify the VPN network namespace.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Blueprint, jsonify, request

bp = Blueprint("vpn_ui", __name__)
GATEWAY = "http://127.0.0.1:8787"
TEST_LOCK = threading.Lock()
test_active = False


def configured():
    return os.getenv("VPN_GATEWAY_ENABLED") == "1"


def permitted():
    token = os.getenv("SURFSHARK_CONTROL_API_KEY", "")
    supplied = request.headers.get("X-VPN-Admin-Token", "")
    return bool(token and len(token) >= 24 and
                hmac.compare_digest(supplied, token))


def gateway_call(path, payload=None):
    if path not in {"/profiles", "/rotate", "/test"} and not (
        path.startswith("/activate/") or path.startswith("/delete/")
    ):
        raise ValueError("Unsupported VPN gateway operation")
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(GATEWAY + path, data=data,
                  headers={"X-VPN-Key": os.getenv("SURFSHARK_CONTROL_API_KEY", ""),
                           "Content-Type": "application/json"},
                  method="POST" if data is not None else "GET")
    try:
        with urlopen(req, timeout=8 if path == "/profiles" else 40) as response:
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
    app.register_blueprint(bp)

    @app.before_request
    def guard_downloads_during_test():
        if not configured() or request.method != "POST":
            return None
        path = request.path
        is_job_change = (path == "/download" or path == "/api/albums"
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
    def auth():
        if not configured():
            return jsonify(enabled=False,
                           error="Managed WireGuard mode is disabled; configure compose.wireguard.yaml"), 503
        if not permitted():
            return jsonify(error="VPN administrator token required"), 403

    @bp.get("/api/vpn/profiles")
    def profiles():
        return gateway_call("/profiles")

    @bp.post("/api/vpn/profiles")
    def upload():
        if not idle(jobs, jobs_lock):
            return jsonify(error="Cannot change VPN profiles with active or queued downloads"), 409
        files = request.files.getlist("configs")
        country = request.form.get("country", "").strip()
        if not files or len(files) > 20:
            return jsonify(error="Select between 1 and 20 WireGuard .conf files"), 400
        results = []
        for f in files:
            payload = f.read(8193)
            if len(payload) > 8192:
                return jsonify(error=f"{f.filename}: file exceeds 8 KB"), 400
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError:
                return jsonify(error=f"{f.filename}: configuration must be UTF-8 text"), 400
            # If multiple countries are uploaded, infer label from filename,
            # unless explicitly supplied for a single file.
            label = country if len(files) == 1 and country else f.filename.removesuffix(".conf")
            item, status = gateway_call("/profiles", {
                "filename": f.filename, "country": label, "content": content})
            if status != 200:
                return jsonify(error=item.get("error", "Upload failed"),
                               uploaded=results), status
            results.append({key: item[key] for key in ("id", "filename", "country")})
        return jsonify(ok=True, uploaded=results)

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
