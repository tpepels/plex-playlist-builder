"""Direct-by-default routing and fail-closed WireGuard teardown regressions."""
from types import SimpleNamespace

import pytest
import vpn_gateway as g


def namespace(tmp_path, monkeypatch, active="a", firewall=True):
    monkeypatch.setattr(g, "ROOT", tmp_path)
    monkeypatch.setattr(g, "ACTIVE_FILE", tmp_path / "wg0.conf")
    monkeypatch.setattr(g, "META_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(g, "FIREWALL_ENABLED", firewall)
    g.TASK.update(running=False, done=True)
    g.save_metadata({
        "profiles": [{"id": "a", "filename": "nl.conf", "country": "NL"},
                     {"id": "b", "filename": "se.conf", "country": "SE"}],
        "active": active,
    })


def test_direct_unblocks_only_after_wireguard_is_gone(tmp_path, monkeypatch):
    namespace(tmp_path, monkeypatch)
    events = []
    monkeypatch.setattr(g, "stop_tunnel",
                        lambda: events.append(("stop_tunnel",)))
    def fake_run(*args, **kwargs):
        events.append(args)
        rc = 1 if args == ("ip", "link", "show", "wg0") else 0
        return SimpleNamespace(returncode=rc)
    monkeypatch.setattr(g, "run", fake_run)
    assert g.direct() == {"direct": True, "mode": "direct"}
    assert g.metadata()["active"] is None
    assert g.FIREWALL_ENABLED is False
    assert events.index(("iptables", "-P", "OUTPUT", "DROP")) < events.index(
        ("stop_tunnel",)
    )
    assert events.index(("stop_tunnel",)) < events.index(
        ("iptables", "-P", "OUTPUT", "ACCEPT")
    )
    assert events.index(("ip6tables", "-P", "OUTPUT", "DROP")) < events.index(
        ("ip6tables", "-P", "OUTPUT", "ACCEPT")
    )


def test_direct_fails_closed_if_tunnel_remains(tmp_path, monkeypatch):
    namespace(tmp_path, monkeypatch)
    events = []
    monkeypatch.setattr(g, "stop_tunnel",
                        lambda: events.append(("stop_tunnel",)))
    monkeypatch.setattr(g, "run", lambda *args, **kwargs: (
        events.append(args) or SimpleNamespace(returncode=0)
    ))
    monkeypatch.setattr(g, "firewall", lambda: events.append(("blocked",)))
    with pytest.raises(g.VPNConnectionError, match="outbound"):
        g.direct()
    assert g.metadata()["active"] == "a"
    assert g.FIREWALL_ENABLED is True
    assert ("iptables", "-P", "OUTPUT", "ACCEPT") not in events


def test_recovery_switches_from_direct_to_different_public_ip(tmp_path, monkeypatch):
    namespace(tmp_path, monkeypatch, active=None, firewall=False)
    selected = []
    monkeypatch.setattr(g, "probe", lambda: {"ip": "198.51.100.7"})
    def activate(pid):
        selected.append(pid)
        data = g.metadata()
        data["active"] = pid
        g.save_metadata(data)
        return {"ip": "198.51.100.7" if pid == "a" else "198.51.100.8",
                "country": pid}
    monkeypatch.setattr(g, "_activate", activate)
    changed = g.rotate()
    assert selected == ["a", "b"]
    assert changed["changed_ip"] is True
    assert changed["profile"]["id"] == "b"


def test_failed_recovery_returns_to_direct(tmp_path, monkeypatch):
    namespace(tmp_path, monkeypatch, active=None, firewall=False)
    monkeypatch.setattr(g, "probe", lambda: {"ip": "198.51.100.7"})
    monkeypatch.setattr(g, "_activate",
                        lambda pid: (_ for _ in ()).throw(
                            g.VPNConnectionError("handshake", "No peer reply")))
    restored = []
    monkeypatch.setattr(g, "_direct", lambda: restored.append(True))
    with pytest.raises(g.VPNConnectionError, match="different verified exit IP"):
        g.rotate()
    assert restored == [True]


def test_startup_always_selects_direct_mode(tmp_path, monkeypatch):
    namespace(tmp_path, monkeypatch, active="a", firewall=False)
    events = []
    monkeypatch.setattr(g, "ensure_control_token",
                        lambda: events.append("token"))
    monkeypatch.setattr(g, "firewall",
                        lambda: events.append("firewall"))
    monkeypatch.setattr(g, "_direct",
                        lambda: events.append("direct"))
    class Server:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 8787)
        def serve_forever(self):
            events.append("serve")
    monkeypatch.setattr(g, "ThreadingHTTPServer", Server)
    g.main()
    assert events == ["token", "firewall", "direct", "serve"]


def test_mode_is_explicit_not_inferred_from_missing_profile(tmp_path, monkeypatch):
    namespace(tmp_path, monkeypatch, active=None, firewall=False)
    monkeypatch.setattr(g, "stored_client_public_key", lambda pid: None)
    status = g.list_profiles()
    assert status["mode"] == "direct"
    assert status["default_mode"] == "direct"
    assert status["kill_switch"] is False
    monkeypatch.setattr(g, "FIREWALL_ENABLED", True)
    status = g.list_profiles()
    assert status["mode"] == "blocked"
    assert status["kill_switch"] is True
