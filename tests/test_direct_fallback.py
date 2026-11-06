"""Network-free verification of cookie filtering and temporary direct egress."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import direct_transport
import youtube_auth


def test_import_keeps_only_youtube_cookies_and_httponly_values():
    source = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tprivate-value\n"
        ".google.com\tTRUE\t/\tTRUE\t0\tGMAIL_AT\tother-secret\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tvisitor-value\n"
    )
    text, count, ignored = youtube_auth.parse_cookies(source.encode())
    assert count == 2 and ignored == 1
    assert "SAPISID\tprivate-value" in text
    assert "GMAIL_AT" not in text
    assert "#HttpOnly_.youtube.com" in text
    assert text.startswith("# Netscape HTTP Cookie File\n")


@pytest.mark.parametrize("data", [
    b"", b"bad\n", b"# Netscape HTTP Cookie File\n",
    b"# Netscape HTTP Cookie File\nnot_a_cookie\n",
    b"# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t0\tX\tbad\n",
    b"x" * (youtube_auth.MAX_COOKIES_SIZE + 1),
])
def test_cookie_import_rejects_empty_invalid_or_no_youtube(data):
    with pytest.raises(ValueError):
        youtube_auth.parse_cookies(data)


def test_cookie_storage_is_private_and_preference_persists(tmp_path, monkeypatch):
    monkeypatch.delenv("COOKIES_FILE", raising=False)
    path = youtube_auth.cookie_path(tmp_path)
    youtube_auth.atomic_private_write(path, "# Netscape HTTP Cookie File\n")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent == tmp_path
    assert youtube_auth.direct_fallback_enabled(tmp_path) is True
    youtube_auth.atomic_private_write(
        youtube_auth.preferences_path(tmp_path),
        json.dumps({"direct_fallback_enabled": False}),
    )
    assert youtube_auth.direct_fallback_enabled(tmp_path) is False


def test_direct_transport_restores_verified_vpn_after_retry():
    calls = []
    def gateway(path, payload=None):
        calls.append(path)
        if path == "/direct/start":
            return {"ok": True, "session": "lease", "vpn_ip": "198.51.100.8"}, 200
        if path == "/direct/end":
            assert payload == {"session": "lease"}
            return {"ok": True, "restored": True}, 200
        if path == "/direct/renew":
            return {"ok": True, "renewed": True}, 200
        raise AssertionError(path)

    with direct_transport.temporary_direct_connection(
        gateway, ip_probe=lambda: "203.0.113.19",
    ) as ip:
        assert ip == "203.0.113.19"
    assert calls == ["/direct/start", "/direct/end"]


def test_direct_transport_rejects_same_ip_but_still_restores():
    calls = []
    def gateway(path, payload=None):
        calls.append(path)
        return (
            ({"session": "lease", "vpn_ip": "198.51.100.8"}, 200)
            if path == "/direct/start"
            else ({"ok": True, "restored": True}, 200)
        )
    with pytest.raises(direct_transport.DirectFallbackError, match="equals"):
        with direct_transport.temporary_direct_connection(
            gateway, ip_probe=lambda: "198.51.100.8",
        ):
            pytest.fail("A same-IP direct download must not start")
    assert calls == ["/direct/start", "/direct/end"]


def test_direct_transport_does_not_run_when_gateway_refuses():
    calls = []
    def gateway(path, payload=None):
        calls.append(path)
        return {"error": "VPN unavailable"}, 400
    with pytest.raises(direct_transport.DirectFallbackError, match="VPN unavailable"):
        with direct_transport.temporary_direct_connection(gateway):
            pytest.fail("Download must not start")
    assert calls == ["/direct/start"]
