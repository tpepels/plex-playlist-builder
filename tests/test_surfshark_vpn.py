"""Short, offline safeguards for the optional Surfshark transport."""
import json
from pathlib import Path

import surfshark_vpn as vpn


def test_rotation_refuses_active_or_queued_downloads():
    assert vpn.busy_jobs([{"status": "queued"}])
    assert vpn.busy_jobs([{"status": "running"}])
    assert vpn.busy_jobs([{"status": "finalizing"}])
    assert vpn.busy_jobs([{"status": "partial", "active_resolution_tracks": ["track"]}])
    assert not vpn.busy_jobs([{"status": "failed"}])
    assert not vpn.busy_jobs([{"status": "needs_metadata"}])


def test_disabled_automatic_rotation_does_nothing(monkeypatch):
    monkeypatch.delenv("SURFSHARK_VPN_ENABLED", raising=False)
    monkeypatch.setenv("SURFSHARK_ROTATE_ON_403", "1")
    assert vpn.rotate_if_enabled() is None
    monkeypatch.setenv("SURFSHARK_VPN_ENABLED", "1")
    monkeypatch.setenv("SURFSHARK_ROTATE_ON_403", "0")
    assert vpn.rotate_if_enabled() is None


def test_reconnect_requires_real_exit_ip_change(monkeypatch):
    calls = []
    current = ["198.51.100.10"]

    def request(endpoint, key, method="GET", payload=None, timeout=5):
        calls.append((endpoint, method, payload))
        if method == "PUT":
            if payload == {"status": "running"}:
                current[0] = "198.51.100.20"
            return {"status": payload["status"]}
        if endpoint == "/v1/vpn/status":
            return {"status": "running"}
        return {"public_ip": current[0]}

    monkeypatch.setattr(vpn, "_request", request)
    result = vpn.rotate("dummy", timeout=1, interval=0)
    assert result == {"changed": True, "old_ip": "198.51.100.10",
                      "new_ip": "198.51.100.20"}
    assert [item[2] for item in calls if item[1] == "PUT"] == [
        {"status": "stopped"}, {"status": "running"},
    ]


def test_same_exit_ip_is_not_reported_as_changed(monkeypatch):
    def request(endpoint, key, method="GET", payload=None, timeout=5):
        return {"status": "running"} if endpoint.endswith("status") else {
            "public_ip": "198.51.100.10"
        }
    monkeypatch.setattr(vpn, "_request", request)
    ticks = iter((0, 0, 3))
    monkeypatch.setattr(vpn.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(vpn.time, "sleep", lambda _seconds: None)
    result = vpn.rotate("dummy", timeout=2, interval=0)
    assert result["changed"] is False
    assert result["new_ip"] == result["old_ip"]


def test_compose_opt_in_and_control_server_not_public():
    root = Path(__file__).parents[1]
    overlay = (root / "compose.surfshark.yaml").read_text()
    assert 'network_mode: "service:surfshark-vpn"' in overlay
    assert 'ports: !reset []' in overlay
    assert '"4545:4545"' in overlay
    assert "8000:8000" not in overlay
    assert 'SURFSHARK_ROTATE_ON_403' in overlay
    assert "SURFSHARK_WIREGUARD_PRIVATE_KEY" in overlay
