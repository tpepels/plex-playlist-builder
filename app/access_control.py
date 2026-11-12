"""Network perimeter for private, single-user Music workspace.

Default deployments accept only RFC1918, loopback, local IPv6 and Tailscale
CGNAT clients. Forwarded-For headers are ignored. Public reverse proxies
must also authenticate users upstream; this protects direct published ports.
"""
from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_address,ip_network
import os

DEFAULT_CIDRS=(
    "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,"
    "192.168.0.0/16,100.64.0.0/10,fc00::/7,fe80::/10"
)


@lru_cache(maxsize=16)
def parse_cidrs(raw):
    if raw.strip()=="*":
        return None  # Explicitly unsafe opt-out, never the default.
    networks=[]
    for segment in raw.split(","):
        text=segment.strip()
        if not text:continue
        networks.append(ip_network(text,strict=False))
    if not networks:
        raise ValueError("WORKSPACE_ALLOWED_CIDRS must contain a trusted subnet.")
    return tuple(networks)


def allowed(client_ip,env=None):
    # TestClient passes a synthetic host rather than a socket peer.
    if client_ip=="testclient":
        return True
    try:
        client=ip_address(client_ip)
        networks=parse_cidrs(env if env is not None else
                             os.getenv("WORKSPACE_ALLOWED_CIDRS",DEFAULT_CIDRS))
        return networks is None or any(client in network for network in networks)
    except (ValueError,TypeError):
        return False
