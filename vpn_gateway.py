"""Fail-closed WireGuard profile gateway for the optional managed VPN mode.

Run only in an isolated NET_ADMIN container with network namespace shared by
yt-dlp-server. The Flask downloader has NO NET_ADMIN permission. Uploaded
configuration is parsed strictly; wg-quick never sees arbitrary hooks.
"""
from __future__ import annotations

import base64
import configparser
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(os.getenv("VPN_PROFILE_DIR", "/profiles"))
ACTIVE_FILE = ROOT / "wg0.conf"
META_FILE = ROOT / "profiles.json"
TOKEN_FILE = Path(os.getenv("VPN_CONTROL_TOKEN_FILE", "/data/state/.vpn-control-token"))
TOKEN = ""
FIREWALL_ENABLED = False
LOCK = threading.RLock()
TASK = {"running": False, "results": [], "message": "Idle", "done": True}
COUNTRY_CODES = {
    "netherlands": "NL", "germany": "DE", "portugal": "PT", "france": "FR",
    "united states": "US", "usa": "US", "united kingdom": "GB", "uk": "GB",
    "belgium": "BE", "spain": "ES", "italy": "IT", "poland": "PL",
    "switzerland": "CH", "sweden": "SE", "norway": "NO", "austria": "AT",
    "finland": "FI", "denmark": "DK", "canada": "CA", "japan": "JP",
    "australia": "AU", "singapore": "SG", "ireland": "IE",
    "greece": "GR", "czech republic": "CZ", "brazil": "BR",
    "romania": "RO", "bulgaria": "BG", "turkey": "TR",
    "south africa": "ZA", "south korea": "KR", "iceland": "IS",
    "new zealand": "NZ", "india": "IN", "indonesia": "ID",
    "hong kong": "HK", "taiwan": "TW",
}
MAX_PROFILES = 20
MAX_BYTES = 8192


def run(*argv, check=True):
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=20)
    if check and result.returncode:
        # Never propagate command output; WireGuard diagnostics may contain keys.
        raise RuntimeError("VPN network command failed: " + argv[0])
    return result


def key(value):
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        raise ValueError("Invalid WireGuard key") from None
    if len(decoded) != 32:
        raise ValueError("Invalid WireGuard key length")
    return value


def parse_profile(content):
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("WireGuard configuration is too large (8 KB max)")
    cfg = configparser.ConfigParser(interpolation=None, strict=True)
    cfg.optionxform = str
    try:
        cfg.read_string(content)
    except configparser.Error:
        raise ValueError("Invalid WireGuard INI format") from None
    if set(cfg.sections()) != {"Interface", "Peer"}:
        raise ValueError("Exactly one [Interface] and one [Peer] are required")
    interface = dict(cfg["Interface"])
    peer = dict(cfg["Peer"])
    # No PreUp/PostUp/SaveConfig/Include or other executable directives.
    if not set(interface) <= {"PrivateKey", "Address", "DNS", "MTU"}:
        raise ValueError("Unsupported or potentially unsafe [Interface] directive")
    if not set(peer) <= {"PublicKey", "PresharedKey", "Endpoint", "AllowedIPs", "PersistentKeepalive"}:
        raise ValueError("Unsupported [Peer] directive")
    private = key(interface.get("PrivateKey", ""))
    public = key(peer.get("PublicKey", ""))
    preshared = key(peer["PresharedKey"]) if peer.get("PresharedKey") else None
    try:
        address = ipaddress.ip_interface(interface["Address"].strip())
    except (KeyError, ValueError):
        raise ValueError("Invalid WireGuard interface address") from None
    if address.version != 4:
        raise ValueError("Only IPv4 WireGuard profiles are supported")
    if peer.get("AllowedIPs", "").replace(" ", "") not in {"0.0.0.0/0", "0.0.0.0/0,::/0"}:
        raise ValueError("Full-tunnel AllowedIPs=0.0.0.0/0 is required")
    endpoint = peer.get("Endpoint", "").strip()
    m = re.fullmatch(r"([A-Za-z0-9.-]{1,253}):(\d{1,5})", endpoint)
    if not m or not (1 <= int(m.group(2)) <= 65535):
        raise ValueError("Invalid WireGuard endpoint host:port")
    hostname, port = m.group(1), int(m.group(2))
    try:
        ipaddress.IPv4Address(hostname)
        endpoint_ip = hostname
    except ipaddress.AddressValueError:
        try:
            endpoint_ip = socket.gethostbyname(hostname)
        except OSError:
            raise ValueError("VPN endpoint hostname could not be resolved") from None
    keepalive = peer.get("PersistentKeepalive", "25")
    if not keepalive.isdigit() or not 0 <= int(keepalive) <= 120:
        raise ValueError("Invalid WireGuard keepalive")
    # Omit DNS from the user config to prevent wg-quick resolver commands.
    lines = [
        "[Interface]",
        "PrivateKey = " + private,
        "Address = " + str(address),
        "",
        "[Peer]",
        "PublicKey = " + public,
    ]
    if preshared:
        lines.append("PresharedKey = " + preshared)
    lines.extend([
        "Endpoint = " + endpoint_ip + ":" + str(port),
        "AllowedIPs = 0.0.0.0/0",
        "PersistentKeepalive = " + keepalive,
        "",
    ])
    return {"text": "\n".join(lines), "endpoint": endpoint_ip, "port": port}


def write_private(path, text):
    temp = ROOT / ("." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def metadata():
    if not META_FILE.exists():
        return {"profiles": [], "active": None}
    return json.loads(META_FILE.read_text())


def save_metadata(value):
    write_private(META_FILE, json.dumps(value, separators=(",", ":")))


def firewall():
    global FIREWALL_ENABLED
    # Once any profile is selected, stay fail-closed until a verified tunnel
    # connects. Fresh installations without profiles retain direct networking.
    run("iptables", "-P", "OUTPUT", "DROP")
    run("iptables", "-F", "OUTPUT")
    run("iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    run("iptables", "-A", "OUTPUT", "-o", "wg0", "-j", "ACCEPT")
    run("iptables", "-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
        "-j", "ACCEPT")
    run("iptables", "-N", "VPN_ENDPOINT", check=False)
    run("iptables", "-F", "VPN_ENDPOINT")
    run("iptables", "-A", "OUTPUT", "-j", "VPN_ENDPOINT")
    run("ip6tables", "-P", "OUTPUT", "DROP")
    run("ip6tables", "-F", "OUTPUT")
    run("ip6tables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    # Keep inbound LAN and Docker-client web sessions reachable after
    # WireGuard installs a default route via wg0.
    for subnet in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        run("ip", "rule", "add", "priority", "100", "to", subnet,
            "lookup", "main", check=False)
    FIREWALL_ENABLED = True


def allow_endpoint(ip, port):
    run("iptables", "-F", "VPN_ENDPOINT")
    run("iptables", "-A", "VPN_ENDPOINT", "-p", "udp", "-d", ip,
        "--dport", str(port), "-j", "ACCEPT")


def stop_tunnel():
    if run("ip", "link", "show", "wg0", check=False).returncode == 0:
        run("wg-quick", "down", str(ACTIVE_FILE))


def activate(profile_id):
    with LOCK:
        if TASK["running"]:
            raise RuntimeError("VPN rotation test is running")
        return _activate(profile_id)


def _activate(profile_id):
    data = metadata()
    choice = next((p for p in data["profiles"] if p["id"] == profile_id), None)
    if not choice:
        raise ValueError("Unknown profile")
    previous = data.get("active")
    previous_text = ACTIVE_FILE.read_text() if ACTIVE_FILE.exists() else None
    candidate = parse_profile((ROOT / (profile_id + ".conf")).read_text())
    if not FIREWALL_ENABLED:
        firewall()
    stop_tunnel()
    allow_endpoint(candidate["endpoint"], candidate["port"])
    write_private(ACTIVE_FILE, candidate["text"])
    try:
        run("wg-quick", "up", str(ACTIVE_FILE))
        check = probe()
        if not check.get("ip"):
            raise RuntimeError("VPN has no verified external IP")
        data["active"] = profile_id
        save_metadata(data)
        return check
    except Exception:
        stop_tunnel()
        # Keep the firewall closed even if restoring the previous tunnel fails.
        data["active"] = None
        save_metadata(data)
        if previous_text and previous:
            try:
                old = parse_profile(previous_text)
                allow_endpoint(old["endpoint"], old["port"])
                write_private(ACTIVE_FILE, old["text"])
                run("wg-quick", "up", str(ACTIVE_FILE))
                data["active"] = previous
                save_metadata(data)
            except Exception:
                pass
        raise RuntimeError("Unable to establish VPN for selected profile") from None


def probe():
    # Internet request is forced through wg0 by the OUTPUT firewall.
    # This public IP-geolocation service can be rate-limited or unavailable.
    for _ in range(3):
        try:
            probe_request = Request("https://ipwho.is/",
                                    headers={"User-Agent": "yt-dlp-wireguard-gateway/1.0",
                                             "Accept": "application/json"})
            with urlopen(probe_request, timeout=7) as response:
                item = json.load(response)
            if item.get("success") and item.get("ip"):
                return {"ip": str(item["ip"]),
                        "country": str(item.get("country") or "Unknown"),
                        "country_code": str(item.get("country_code") or "")}
        except (OSError, ValueError):
            time.sleep(1)
    raise RuntimeError("VPN external IP test failed (network or geolocation unavailable)")


def expected_country_code(label):
    value = label.strip().lower()
    if value in COUNTRY_CODES:
        return COUNTRY_CODES[value]
    if re.fullmatch(r"[a-z]{2}", value):
        return "GB" if value == "uk" else value.upper()
    match = re.match(r"([a-z]{2})[-_.]", value)
    if match:
        code = match.group(1).upper()
        return "GB" if code == "UK" else code
    return None


def list_profiles():
    # Never block status polling on a slow VPN handshake or geolocation
    # request. Metadata writes are atomic file replacements and task fields
    # are independent small Python values.
    value = metadata()
    status = {"running": TASK["running"], "done": TASK["done"],
              "message": TASK["message"], "results": list(TASK["results"])}
    return {"profiles": value["profiles"], "active": value["active"],
            "test": status}


def add_profile(filename, country, content):
    with LOCK:
        if TASK["running"]:
            raise RuntimeError("VPN rotation test is running")
        parsed = parse_profile(content)
        data = metadata()
        if len(data["profiles"]) >= MAX_PROFILES:
            raise ValueError("Maximum 20 profiles reached")
        if not re.fullmatch(r"[A-Za-z0-9_. -]{1,80}\.conf", filename):
            raise ValueError("Invalid .conf filename")
        country = country.strip()
        if not 1 <= len(country) <= 60 or not re.fullmatch(r"[\w .()-]+", country):
            raise ValueError("Enter a country label (max 60 characters)")
        profile_id = uuid.uuid4().hex
        write_private(ROOT / (profile_id + ".conf"), parsed["text"])
        data["profiles"].append({"id": profile_id, "filename": filename,
                                 "country": country})
        save_metadata(data)
        return {"id": profile_id, "filename": filename, "country": country}


def delete_profile(profile_id):
    with LOCK:
        if TASK["running"]:
            raise RuntimeError("VPN rotation test is running")
        data = metadata()
        if data["active"] == profile_id:
            raise ValueError("Cannot delete the active VPN profile")
        match = [p for p in data["profiles"] if p["id"] == profile_id]
        if not match:
            raise ValueError("Unknown profile")
        data["profiles"] = [p for p in data["profiles"] if p["id"] != profile_id]
        save_metadata(data)
        (ROOT / (profile_id + ".conf")).unlink(missing_ok=True)
        return {"deleted": True}


def rotate():
    with LOCK:
        if TASK["running"]:
            raise RuntimeError("VPN rotation test is running")
        data = metadata()
        choices = data["profiles"]
        if not choices:
            raise ValueError("Upload a WireGuard profile first")
        current = next((i for i,p in enumerate(choices)
                        if p["id"] == data["active"]), -1)
        target = choices[(current + 1) % len(choices)]
        previous = data.get("active")
        old_ip = probe()["ip"] if previous else None
        result = _activate(target["id"])
        changed_ip = old_ip is None or old_ip != result["ip"]
        return {"profile": target, **result,
                "changed_profile": previous != target["id"],
                "changed_ip": changed_ip}


def test_worker():
    with LOCK:
        original = metadata().get("active")
        choices = metadata()["profiles"]
    previous_ip = None
    try:
        for entry in choices:
            try:
                with LOCK:
                    result = _activate(entry["id"])
                    changed_ip = previous_ip is None or previous_ip != result["ip"]
                    expected = expected_country_code(entry["country"])
                    country_match = expected is not None and expected == result.get("country_code")
                    ok = changed_ip and country_match
                    if not changed_ip:
                        note = "Exit IP did not change"
                    elif expected is None:
                        note = "Unknown expected country; use a country name or ISO code"
                    elif not country_match:
                        note = "Expected " + expected + "; observed " + str(result.get("country_code") or "?")
                    else:
                        note = "Verified country and exit IP"
                    item = {"filename": entry["filename"], "country": entry["country"],
                            **result, "ok": ok, "message": note}
                    previous_ip = result["ip"]
                    TASK["results"].append(item)
                    TASK["message"] = "Tested " + entry["filename"]
            except Exception as exc:
                with LOCK:
                    TASK["results"].append({"filename": entry["filename"],
                                            "country": entry["country"], "ok": False,
                                            "message": str(exc)})
        with LOCK:
            TASK["message"] = "Restoring previous VPN state"
            if original:
                _activate(original)
            else:
                stop_tunnel()
                data = metadata()
                data["active"] = None
                save_metadata(data)
                ACTIVE_FILE.unlink(missing_ok=True)
                run("iptables", "-F", "VPN_ENDPOINT")
    except Exception:
        with LOCK:
            TASK["message"] = "Could not restore the previous VPN profile"
    finally:
        with LOCK:
            TASK["running"] = False
            TASK["done"] = True
            if TASK["message"].startswith("Tested ") or TASK["message"].startswith("Restoring "):
                TASK["message"] = "Rotation test finished"


def start_test():
    with LOCK:
        if TASK["running"]:
            raise RuntimeError("A rotation test is already running")
        if not metadata()["profiles"]:
            raise ValueError("Upload profiles first")
        TASK.update(running=True, done=False, results=[], message="Starting rotation test")
        threading.Thread(target=test_worker, daemon=True).start()
        return {"started": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return  # Do not log credentials or uploaded configuration.

    def reply(self, code, obj):
        encoded = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def handle_one(self):
        if not TOKEN or self.headers.get("X-VPN-Key") != TOKEN:
            return self.reply(403, {"error": "VPN administrator authentication required"})
        if self.command == "GET" and self.path == "/profiles":
            return self.reply(200, list_profiles())
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400, {"error": "Invalid request length"})
            if length <= 0 or length > 20000:
                return self.reply(413, {"error": "Invalid upload size"})
            try:
                payload = json.loads(self.rfile.read(length))
                if self.path == "/profiles":
                    result = add_profile(payload["filename"], payload["country"],
                                         payload["content"])
                elif self.path == "/rotate":
                    result = rotate()
                elif self.path == "/test":
                    result = start_test()
                elif re.fullmatch(r"/activate/[0-9a-f]{32}", self.path):
                    result = activate(self.path.split("/")[-1])
                elif re.fullmatch(r"/delete/[0-9a-f]{32}", self.path):
                    result = delete_profile(self.path.split("/")[-1])
                else:
                    return self.reply(404, {"error": "Not found"})
                return self.reply(200, {"ok": True, **result})
            except (ValueError, KeyError, RuntimeError, TypeError) as exc:
                return self.reply(400, {"error": str(exc)})
        return self.reply(404, {"error": "Not found"})

    def do_GET(self):
        self.handle_one()

    def do_POST(self):
        self.handle_one()


def ensure_control_token():
    """Create a private, persistent control token readable by the downloader UID.

    This file is on the already-mounted downloader state volume; it never
    crosses the browser and never needs to be copied into an .env file.
    """
    global TOKEN
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if len(token) < 32:
            raise RuntimeError("Existing VPN control token is invalid")
    else:
        token = secrets.token_urlsafe(36)
        try:
            fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            token = TOKEN_FILE.read_text().strip()
        else:
            with os.fdopen(fd, "w") as f:
                f.write(token + "\n")
    if len(token) < 32:
        raise RuntimeError("VPN control token could not be initialized")
    os.chmod(TOKEN_FILE, 0o600)
    # The downloader is unprivileged. It reads this one file but receives
    # no raw uploaded VPN profiles and no capability to alter networking.
    uid = int(os.getenv("VPN_DOWNLOADER_UID", "1000"))
    gid = int(os.getenv("VPN_DOWNLOADER_GID", "1000"))
    os.chown(TOKEN_FILE, uid, gid)
    TOKEN = token


def main():
    ensure_control_token()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    # Downloads work normally before the user has uploaded any VPN profile.
    # Once a VPN is configured, restarts always fail closed until it connects.
    if metadata()["profiles"]:
        firewall()
    if ACTIVE_FILE.exists() and metadata().get("active"):
        try:
            # Permit the persisted endpoint and restore the VPN on restart.
            active = parse_profile(ACTIVE_FILE.read_text())
            allow_endpoint(active["endpoint"], active["port"])
            run("wg-quick", "up", str(ACTIVE_FILE))
        except Exception:
            # Fail closed: downloads cannot egress until a valid profile connects.
            pass
    ThreadingHTTPServer(("127.0.0.1", 8787), Handler).serve_forever()


if __name__ == "__main__":
    main()
