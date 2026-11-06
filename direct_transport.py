"""Short-lived direct-egress lease for one bot-blocked download attempt.

The privileged gateway owns network changes; this module owns only the
authenticated lease lifecycle and independent public-IP verification.
"""
from __future__ import annotations

import ipaddress
import threading
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


class DirectFallbackError(RuntimeError):
    pass


def public_ipv4():
    for url in ("https://api.ipify.org", "https://ipv4.icanhazip.com"):
        try:
            with urlopen(url, timeout=8) as response:
                value = response.read(64).decode("ascii").strip()
            return str(ipaddress.IPv4Address(value))
        except (HTTPError, URLError, OSError, ValueError, UnicodeDecodeError):
            continue
    raise DirectFallbackError("Could not verify direct public IPv4; no direct retry was started")


@contextmanager
def temporary_direct_connection(gateway_call, ip_probe=public_ipv4):
    """Temporarily allow the downloader UID to exit directly.

    A gateway watchdog revokes the permission when this process crashes.
    A heartbeat renews the short lease for downloads taking over 3 minutes.
    The exit rule is removed and WireGuard is restored before returning.
    """
    started, status = gateway_call("/direct/start", {})
    if status != 200 or not started.get("session"):
        raise DirectFallbackError(started.get("error") or "VPN gateway refused direct fallback")
    lease = started["session"]
    stop = threading.Event()
    expired = threading.Event()

    def heartbeat():
        while not stop.wait(25):
            result, code = gateway_call("/direct/renew", {"session": lease})
            if code != 200 or not result.get("ok"):
                expired.set()
                return

    keepalive = threading.Thread(target=heartbeat, name="direct-fallback-lease", daemon=True)
    keepalive.start()
    try:
        direct_ip = ip_probe()
        if direct_ip == started.get("vpn_ip"):
            raise DirectFallbackError("Direct exit IP equals the VPN exit; refusing redundant retry")
        yield direct_ip
        if expired.is_set():
            raise DirectFallbackError("Direct fallback lease expired during download")
    finally:
        stop.set()
        keepalive.join(timeout=1)
        result, code = gateway_call("/direct/end", {"session": lease})
        if code != 200 or not result.get("restored"):
            raise DirectFallbackError(
                "VPN restoration could not be verified; direct egress was revoked"
            )
