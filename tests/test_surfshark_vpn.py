"""Managed single-container WireGuard transport tests."""
import io
from pathlib import Path

import pytest
import surfshark_vpn as vpn


def test_busy_downloads_prevent_manual_vpn_changes():
    for status in ("queued", "running", "finalizing"):
        assert vpn.busy_jobs([{"status": status}])
    assert vpn.busy_jobs([{"status": "partial", "active_resolution_tracks": ["x"]}])
    assert not vpn.busy_jobs([{"status": "failed"}])
    assert not vpn.busy_jobs([{"status": "needs_metadata"}])


def test_disabled_transport_never_contacts_gateway(monkeypatch):
    monkeypatch.delenv("VPN_GATEWAY_ENABLED", raising=False)
    monkeypatch.setenv("SURFSHARK_ROTATE_ON_403", "1")
    monkeypatch.setattr(vpn, "urlopen", lambda *a, **k: pytest.fail("unexpected"))
    assert vpn.direct_if_enabled() is None
    assert vpn.rotate_if_enabled() is None


def test_managed_gateway_uses_token_and_reports_verified_ip(monkeypatch, tmp_path):
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    monkeypatch.delenv("SURFSHARK_ROTATE_ON_403", raising=False)
    key = tmp_path / "token"
    secret = "private-control-token-at-least-32-characters"
    key.write_text(secret)
    monkeypatch.setenv("VPN_CONTROL_TOKEN_FILE", str(key))
    requests = []

    class Reply:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return io.BytesIO(self.payload)
        def __exit__(self, *_):
            return False

    def request(req, timeout):
        requests.append((req.full_url, req.get_method(), req.get_header("X-vpn-key"), timeout))
        if req.full_url.endswith("/rotate"):
            return Reply(b'{"changed_ip":true,"ip":"198.51.100.9","country":"Sweden"}')
        return Reply(b'{"direct":true,"mode":"direct"}')

    monkeypatch.setattr(vpn, "urlopen", request)
    assert vpn.rotate_if_enabled() == {
        "changed": True, "new_ip": "198.51.100.9", "country": "Sweden"
    }
    assert vpn.direct_if_enabled() == {"direct": True}
    assert [request[0] for request in requests] == [
        "http://127.0.0.1:8787/rotate", "http://127.0.0.1:8787/direct"
    ]
    assert all(item[2] == secret for item in requests)
    assert [item[3] for item in requests] == [240, 30]


def test_explicit_opt_out_blocks_rotation_only(monkeypatch):
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    monkeypatch.setenv("SURFSHARK_ROTATE_ON_403", "0")
    monkeypatch.setattr(vpn, "urlopen", lambda *a, **k: pytest.fail("unexpected"))
    assert vpn.rotate_if_enabled() is None


def test_failed_gateway_call_keeps_direct_restoration_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    path = tmp_path / "token"
    path.write_text("valid-server-token-longer-than-32-characters")
    monkeypatch.setenv("VPN_CONTROL_TOKEN_FILE", str(path))
    monkeypatch.setattr(vpn, "urlopen",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("unreachable")))
    assert vpn.direct_if_enabled()["direct"] is False
    assert vpn.rotate_if_enabled()["changed"] is False


def test_no_token_fails_closed_without_exposing_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("VPN_GATEWAY_ENABLED", "1")
    monkeypatch.setenv("VPN_CONTROL_TOKEN_FILE", str(tmp_path / "missing"))
    monkeypatch.setattr(vpn, "urlopen", lambda *a, **k: pytest.fail("unexpected"))
    assert vpn.direct_if_enabled()["direct"] is False


def test_canonical_compose_enables_managed_transport_without_optional_overrides():
    root = Path(__file__).parents[1]
    for name in ("compose.yaml", "compose.parent-vpn.yaml"):
        data = (root / name).read_text()
        assert 'SURFSHARK_ROTATE_ON_403: ${SURFSHARK_ROTATE_ON_403:-1}' in data
        assert 'VPN_GATEWAY_ENABLED: "1"' in data
    assert not (root / "compose.surfshark.yaml").exists()
    assert not (root / "compose.wireguard.yaml").exists()
