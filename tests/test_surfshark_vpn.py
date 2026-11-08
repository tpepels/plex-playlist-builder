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
    monkeypatch.delenv("VPN_GATEWAY_ENABLED", raising=False)
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


def test_gateway_403_rotation_enabled_by_default(monkeypatch, tmp_path):
    import io

    monkeypatch.delenv("SURFSHARK_ROTATE_ON_403", raising=False)
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    monkeypatch.delenv("SURFSHARK_VPN_ENABLED", raising=False)
    token = tmp_path / "control-token"
    token.write_text("test-control-token-with-more-than-32-characters")
    monkeypatch.setenv("VPN_CONTROL_TOKEN_FILE", str(token))

    calls = []
    class Reply:
        def __enter__(self): return io.BytesIO(
            b'{"ok":true,"changed_ip":true,"country":"Sweden","ip":"198.51.100.6"}'
        )
        def __exit__(self, *args): return False

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.get_method(), timeout))
        return Reply()

    monkeypatch.setattr(vpn, "urlopen", fake_urlopen)
    result = vpn.rotate_if_enabled()
    assert result == {"changed": True, "new_ip": "198.51.100.6",
                      "country": "Sweden"}
    assert calls == [("http://127.0.0.1:8787/rotate", "POST", 240)]


def test_explicit_zero_disables_default_rotation(monkeypatch):
    monkeypatch.setenv("SURFSHARK_ROTATE_ON_403", "0")
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    def no_network(*args, **kwargs):
        raise AssertionError("rotation network call attempted despite 0")
    monkeypatch.setattr(vpn, "urlopen", no_network)
    assert vpn.rotate_if_enabled() is None


def test_compose_managed_gateway_defaults_to_rotation_without_env_override():
    root = Path(__file__).parents[1]
    for path in ("compose.yaml", "compose.parent-vpn.yaml",
                 "compose.surfshark.yaml"):
        source = (root / path).read_text()
        assert "SURFSHARK_ROTATE_ON_403: ${SURFSHARK_ROTATE_ON_403:-1}" in source
    assert 'os.getenv("SURFSHARK_ROTATE_ON_403", "1")' in Path(
        vpn.__file__).read_text()


def test_managed_gateway_restores_direct_with_authenticated_control(monkeypatch, tmp_path):
    import io
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    token = tmp_path / "control"
    token.write_text("internal-gateway-token-longer-than-32-characters")
    monkeypatch.setenv("VPN_CONTROL_TOKEN_FILE", str(token))
    requests = []

    class Reply:
        def __enter__(self):
            return io.BytesIO(b'{"ok":true,"direct":true,"mode":"direct"}')
        def __exit__(self, *_):
            return False

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, request.get_method(), timeout))
        return Reply()

    monkeypatch.setattr(vpn, "urlopen", fake_urlopen)
    assert vpn.direct_if_enabled() == {"direct": True}
    assert requests == [("http://127.0.0.1:8787/direct", "POST", 30)]


def test_direct_restoration_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    token = tmp_path / "control"
    token.write_text("internal-gateway-token-longer-than-32-characters")
    monkeypatch.setenv("VPN_CONTROL_TOKEN_FILE", str(token))
    monkeypatch.setattr(vpn, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("offline")))
    result = vpn.direct_if_enabled()
    assert result["direct"] is False
    assert "Could not restore" in result["error"]


def test_no_managed_gateway_does_not_force_network_switch(monkeypatch):
    monkeypatch.delenv("VPN_GATEWAY_ENABLED", raising=False)
    assert vpn.direct_if_enabled() is None
