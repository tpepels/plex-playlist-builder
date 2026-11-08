"""Authenticated Gluetun status and bounded VPN reconnect.

Works only inside the VPN-networked downloader container. Never publishes
Gluetun's HTTP control port and never accepts an arbitrary endpoint URL.
"""
from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GLUETUN_CONTROL = "http://127.0.0.1:8000"
ACTIVE = frozenset({"queued", "running", "finalizing"})


def _request(endpoint, key, method="GET", payload=None, timeout=5):
    if not key:
        raise RuntimeError("Surfshark control API key is not configured.")
    if endpoint not in {"/v1/vpn/status", "/v1/publicip/ip"}:
        raise ValueError("Control API endpoint not permitted")
    headers = {"X-API-Key": key}
    encoded = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        encoded = json.dumps(payload).encode("utf-8")
    req = Request(GLUETUN_CONTROL + endpoint, headers=headers,
                  method=method, data=encoded)
    try:
        with urlopen(req, timeout=timeout) as response:
            content = response.read(4096)
    except (URLError, HTTPError, OSError) as exc:
        raise RuntimeError(f"VPN control connection failed: {type(exc).__name__}") from None
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("VPN control response was not valid JSON.") from None


def vpn_status(key):
    status = _request("/v1/vpn/status", key)
    ip_result = _request("/v1/publicip/ip", key)
    return {
        "status": str(status.get("status") or "unknown"),
        "ip": str(ip_result.get("public_ip") or ""),
    }


def busy_jobs(jobs):
    """Do not interrupt an active worker or a queued job (race avoidance)."""
    return any(
        item.get("status") in ACTIVE or item.get("active_resolution_tracks")
        for item in jobs
    )


def rotate(key, *, timeout=50, interval=2):
    """Reconnect once and verify public IP really changed.

    A WireGuard reconnect may choose the same endpoint; that must not be
    reported as a successful IP change.
    """
    old = vpn_status(key)
    if old["status"] != "running" or not old["ip"]:
        raise RuntimeError("VPN was not confirmed running with a public IP.")
    _request("/v1/vpn/status", key, method="PUT", payload={"status": "stopped"})
    # Always attempt restart even if status checks fail while disconnected.
    _request("/v1/vpn/status", key, method="PUT", payload={"status": "running"})
    deadline = time.monotonic() + timeout
    current = {"status": "unknown", "ip": ""}
    while time.monotonic() < deadline:
        try:
            current = vpn_status(key)
            if current["status"] == "running" and current["ip"]:
                if current["ip"] != old["ip"]:
                    return {
                        "changed": True, "old_ip": old["ip"],
                        "new_ip": current["ip"],
                    }
        except RuntimeError:
            pass
        time.sleep(interval)
    if current["status"] != "running":
        raise RuntimeError("VPN did not confirm recovery after reconnect.")
    return {"changed": False, "old_ip": old["ip"],
            "new_ip": current["ip"]}


def _gateway_request(operation, *, timeout):
    """Authenticated loopback transport shared by retry and direct restoration."""
    if operation not in {"/rotate", "/direct"}:
        raise ValueError("Unsupported gateway action")
    try:
        key = __import__("pathlib").Path(
            os.getenv("VPN_CONTROL_TOKEN_FILE", "/data/state/.vpn-control-token")
        ).read_text().strip()
        if len(key) < 32:
            raise ValueError("Invalid gateway token")
    except (OSError, ValueError):
        raise RuntimeError("VPN gateway control token is unavailable") from None
    request = Request(
        "http://127.0.0.1:8787" + operation,
        data=b"{}", method="POST",
        headers={"X-VPN-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (RuntimeError, URLError, HTTPError, TimeoutError, ValueError, OSError):
        raise RuntimeError("VPN gateway control request failed") from None


def direct_if_enabled():
    """Return the managed gateway to direct routing (also after failed retries).

    The legacy Gluetun transport is not capable of direct-first operation;
    its network policy is not changed here.
    """
    if os.getenv("VPN_GATEWAY_ENABLED") != "1":
        return None
    try:
        response = _gateway_request("/direct", timeout=30)
        return {"direct": response.get("direct") is True}
    except RuntimeError:
        return {"direct": False, "error": "Could not restore the direct connection"}


def rotate_if_enabled():
    """Connect to a verified VPN exit *only after* a selected YouTube failure."""
    if os.getenv("SURFSHARK_ROTATE_ON_403", "1") != "1":
        return None
    if os.getenv("VPN_GATEWAY_ENABLED") == "1":
        try:
            obj = _gateway_request("/rotate", timeout=240)
            return {"changed": bool(obj.get("changed_ip")),
                    "new_ip": obj.get("ip", ""), "country": obj.get("country", "")}
        except RuntimeError:
            return {"changed": False, "error": "VPN profile rotation failed."}
    if os.getenv("SURFSHARK_VPN_ENABLED") != "1":
        return None
    try:
        return rotate(os.getenv("SURFSHARK_CONTROL_API_KEY"), timeout=45)
    except RuntimeError as exc:
        return {"changed": False, "error": str(exc)}


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Inspect Surfshark egress or reconnect between downloads.")
    parser.add_argument("command", choices=("status", "rotate"))
    args = parser.parse_args()

    if os.getenv("SURFSHARK_VPN_ENABLED") != "1":
        parser.error("VPN mode is disabled. Use compose.surfshark.yaml.")
    key = os.getenv("SURFSHARK_CONTROL_API_KEY")
    try:
        if args.command == "status":
            print(json.dumps(vpn_status(key), sort_keys=True))
            return 0
        jobs_req = Request("http://127.0.0.1:4545/api/jobs")
        with urlopen(jobs_req, timeout=5) as response:
            jobs = json.load(response)
        if not isinstance(jobs, list):
            raise RuntimeError("Cannot confirm downloader is idle.")
        if busy_jobs(jobs):
            raise RuntimeError("Download(s) active or queued; not reconnecting VPN.")
        result = rotate(key)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["changed"] else 2
    except (RuntimeError, URLError, HTTPError, OSError, ValueError) as exc:
        print(f"Surfshark: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
