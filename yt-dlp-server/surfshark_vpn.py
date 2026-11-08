"""Authenticated, loopback-only control for the managed WireGuard gateway.

Both services are OS processes inside the same container. Only the gateway
process runs with NET_ADMIN; download workers never modify host networking.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ACTIVE = frozenset({"queued", "running", "finalizing"})
GATEWAY = "http://127.0.0.1:8787"


def busy_jobs(jobs):
    return any(
        item.get("status") in ACTIVE or item.get("active_resolution_tracks")
        for item in jobs
    )


def _gateway_request(operation, *, timeout):
    if operation not in {"/rotate", "/direct"}:
        raise ValueError("Unsupported gateway action")
    try:
        key = Path(os.getenv(
            "VPN_CONTROL_TOKEN_FILE", "/data/state/.vpn-control-token"
        )).read_text().strip()
        if len(key) < 32:
            raise ValueError("Invalid gateway token")
    except (OSError, ValueError):
        raise RuntimeError("VPN gateway control token is unavailable") from None
    req = Request(
        GATEWAY + operation, data=b"{}", method="POST",
        headers={"X-VPN-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except (RuntimeError, URLError, HTTPError, TimeoutError, ValueError, OSError):
        raise RuntimeError("VPN gateway control request failed") from None


def direct_if_enabled():
    if os.getenv("VPN_GATEWAY_ENABLED") != "1":
        return None
    try:
        result = _gateway_request("/direct", timeout=30)
        return {"direct": result.get("direct") is True}
    except RuntimeError:
        return {"direct": False, "error": "Could not restore the direct connection"}


def rotate_if_enabled():
    """Only request a VPN exit after a qualifying downloader failure."""
    if os.getenv("VPN_GATEWAY_ENABLED") != "1":
        return None
    if os.getenv("SURFSHARK_ROTATE_ON_403", "1") != "1":
        return None
    try:
        result = _gateway_request("/rotate", timeout=240)
        return {
            "changed": bool(result.get("changed_ip")),
            "new_ip": result.get("ip", ""),
            "country": result.get("country", ""),
        }
    except RuntimeError:
        return {"changed": False, "error": "VPN profile rotation failed."}
