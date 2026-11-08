"""On-demand WireGuard profile gateway with direct-by-default outbound routing.

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
from urllib.error import HTTPError, URLError
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
# Cache only the derived PUBLIC half. Do not cache private-key strings.
PROFILE_PUBLIC_CACHE = {}
# Reuse wg-quick's fwmark to exempt the HTTP listener's reply packets from
# its default-route policy without granting general direct Internet access.
WG_FWMARK = 51820
WEB_PORT = 4545


class VPNConnectionError(RuntimeError):
    """Safe, user-visible connection stage; never contains config or keys."""

    def __init__(self, phase, message):
        self.phase = phase
        super().__init__(message)


def setup_error(stderr):
    """Classify only known error patterns. Never return raw command stderr."""
    value = stderr.lower()
    if "operation not permitted" in value or "permission denied" in value:
        return VPNConnectionError(
            "permissions", "WireGuard cannot start: container lacks required "
            "network permissions. Check NET_ADMIN and /dev/net/tun.")
    if "operation not supported" in value or "protocol not supported" in value:
        return VPNConnectionError(
            "kernel", "WireGuard is unavailable in the host kernel or container.")
    if "nft: command not found" in value or "iptables-restore: command not found" in value:
        return VPNConnectionError(
            "firewall", "WireGuard routing tools are missing from the gateway image.")
    if "file exists" in value:
        return VPNConnectionError(
            "routing", "WireGuard could not create its route or interface "
            "because a previous route already exists.")
    if "resolvconf: not found" in value:
        return VPNConnectionError(
            "dns_setup", "WireGuard requires a DNS helper missing from this image.")
    return VPNConnectionError(
        "tunnel_setup", "WireGuard could not create the tunnel. Inspect the "
        "gateway's network capabilities; no VPN credentials are shown.")


def has_handshake():
    """Only inspect handshake timestamps, never print public or private keys."""
    output = run("wg", "show", "wg0", "latest-handshakes", check=False)
    if output.returncode:
        return False
    for line in output.stdout.splitlines():
        values = line.split()
        if len(values) == 2 and values[1].isdigit() and int(values[1]) > 0:
            return True
    return False


def endpoint_udp_packets(port):
    """Packets accepted by the endpoint firewall rule, if readable.

    iptables-legacy/nft format differently. No command output is returned.
    This is a local OUTPUT-filter counter, not proof of remote receipt.
    """
    try:
        status = run("iptables", "-w", "-t", "filter", "-L",
                     "VPN_ENDPOINT", "-v", "-n", "-x", check=False)
        if status.returncode:
            return None
        for line in status.stdout.splitlines():
            fields = line.split()
            if (fields and fields[0].isdigit() and "udp" in fields
                    and "dpt:" + str(port) in line):
                return int(fields[0])
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return None
    return None


def wireguard_transfer():
    """Read WireGuard's own peer transfer counters, excluding all peer keys.

    A positive transmitted byte count proves that the local WireGuard
    interface tried sending traffic, not that the endpoint received it.
    """
    try:
        state = run("wg", "show", "wg0", "transfer", check=False)
        if state.returncode:
            return None
        received = transmitted = 0
        seen = False
        for line in state.stdout.splitlines():
            columns = line.split()
            if (len(columns) != 3 or
                    not columns[1].isdigit() or not columns[2].isdigit()):
                continue
            seen = True
            received += int(columns[1])
            transmitted += int(columns[2])
        return {"rx_bytes": received, "tx_bytes": transmitted} if seen else None
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None


def endpoint_route(endpoint):
    """Check the underlay route selected for the WireGuard-marked UDP socket.

    wg-quick uses an fwmark to keep its UDP transport out of its own tunnel.
    Return only a bounded classification, never raw routes or local addresses.
    """
    try:
        mark_state = run("wg", "show", "wg0", "fwmark", check=False)
        if mark_state.returncode:
            return "unavailable"
        mark = mark_state.stdout.strip()
        if not re.fullmatch(r"(?:0x[0-9a-fA-F]+|[0-9]+)", mark):
            return "unavailable"
        route = run("ip", "-4", "route", "get", endpoint, "mark", mark,
                    check=False)
        if route.returncode:
            return "unavailable"
        words = route.stdout.split()
        if "dev" not in words:
            return "unavailable"
        interface = words[words.index("dev") + 1]
        return "tunnel" if interface == "wg0" else "underlay"
    except (RuntimeError, ValueError, IndexError, OSError,
            subprocess.TimeoutExpired):
        return "unavailable"


def client_public_key(profile_text):
    """Return only the public half of the uploaded registered keypair."""
    try:
        line = next(line for line in profile_text.splitlines()
                    if line.startswith("PrivateKey = "))
        secret = line.split("=", 1)[1].strip()
        public = subprocess.run(
            ["wg", "pubkey"], input=secret + "\n", text=True,
            capture_output=True, check=False, timeout=4,
        )
        if public.returncode == 0:
            # Prevent arbitrary command output from being exposed to the UI.
            result = public.stdout.strip()
            if re.fullmatch(r"[A-Za-z0-9+/]{43}=", result):
                return result
    except (StopIteration, OSError, subprocess.TimeoutExpired):
        pass
    return None


def safe_handshake_diagnostics(candidate):
    transfer = wireguard_transfer()
    return {
        "endpoint": candidate["endpoint"],
        "port": candidate["port"],
        "udp_packets_allowed": endpoint_udp_packets(candidate["port"]),
        "wireguard_tx_bytes": transfer["tx_bytes"] if transfer else None,
        "wireguard_rx_bytes": transfer["rx_bytes"] if transfer else None,
        "endpoint_route": endpoint_route(candidate["endpoint"]),
        "client_public_key": client_public_key(candidate["text"]),
    }


def wait_for_handshake(seconds=13):
    """Initiate traffic via the tunnel then require an actual WG peer response.

    Numeric IPv4 bypasses Docker DNS. It is used solely to trigger the VPN
    handshake; successful HTTPS geolocation remains a separate requirement.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if has_handshake():
            return
        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=1.4):
                pass
        except OSError:
            # TCP may fail when there is no tunnel; the WG handshake still runs.
            pass
        if has_handshake():
            return
        time.sleep(0.5)
    raise VPNConnectionError(
        "handshake", "WireGuard interface started, but the Surfshark server "
        "did not complete a handshake. Check that the private key is registered "
        "with Surfshark and that the host allows UDP port 51820.")


def check_dns():
    try:
        socket.getaddrinfo("ipwho.is", 443, family=socket.AF_INET)
    except OSError:
        raise VPNConnectionError(
            "dns", "WireGuard handshake succeeded, but DNS lookup failed. "
            "The gateway cannot resolve public-IP verification hosts.") from None


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
    if not set(interface) <= {"PrivateKey", "Address", "DNS", "MTU", "FwMark"}:
        raise ValueError("Unsupported or potentially unsafe [Interface] directive")
    # This fixed mark is used by both wg-quick policy routing and the narrow
    # web-response mangle rule. Never accept a caller-selected alternate mark.
    if interface.get("FwMark") not in (None, str(WG_FWMARK)):
        raise ValueError("Unsupported WireGuard FwMark value")
    if not set(peer) <= {"PublicKey", "PresharedKey", "Endpoint", "AllowedIPs", "PersistentKeepalive"}:
        raise ValueError("Unsupported [Peer] directive")
    provided_private = interface.get("PrivateKey", "").strip()
    if provided_private == "<insert_your_private_key_here>":
        raise ValueError(
            "Surfshark profile is a template. Obtain your registered WireGuard "
            "client private key from Surfshark and enter it once in the uploader."
        )
    private = key(provided_private)
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
        "FwMark = " + str(WG_FWMARK),
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
    # wg-quick installs "not fwmark WG_FWMARK -> wg0" before any existing
    # lower-priority source-port rule. Mark only TCP RESPONSE packets from
    # port 4545, so they bypass that wg0 route and use Docker's underlay.
    # Adding a low-priority ip rule cannot work reliably: wg-quick inserts
    # its own routing rules before it, as demonstrated by 48/49 before 50.
    mark_match = ("-p", "tcp", "--sport", str(WEB_PORT),
                  "-j", "MARK", "--set-mark", str(WG_FWMARK))
    present = run("iptables", "-t", "mangle", "-C", "OUTPUT",
                  *mark_match, check=False)
    if present.returncode:
        run("iptables", "-t", "mangle", "-I", "OUTPUT", "1",
            *mark_match)
    # Once any profile is selected, stay fail-closed until a verified tunnel
    # connects. Fresh installations without profiles retain direct networking.
    run("iptables", "-P", "OUTPUT", "DROP")
    FIREWALL_ENABLED = True  # State becomes blocked immediately, including on partial setup failure.
    run("iptables", "-F", "OUTPUT")
    run("iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    # Preserve the separately published web UI even when the tunnel is down.
    # Docker proxy/NAT can classify the initial server reply as NEW rather
    # than ESTABLISHED. An explicit SOURCE-port exception is necessary.
    # Only the web server's RESPONSE port is allowed: new outbound downloader
    # connections still have ephemeral source ports and remain blocked.
    run("iptables", "-A", "OUTPUT", "-p", "tcp", "--sport", "4545",
        "-j", "ACCEPT")
    run("iptables", "-A", "OUTPUT", "-o", "wg0", "-j", "ACCEPT")
    # Do not allow already-established non-VPN Internet connections to leak.
    # Permit replies for local Docker/LAN clients only.
    for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        run("iptables", "-A", "OUTPUT", "-d", private,
            "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
    run("iptables", "-N", "VPN_ENDPOINT", check=False)
    run("iptables", "-F", "VPN_ENDPOINT")
    run("iptables", "-A", "OUTPUT", "-j", "VPN_ENDPOINT")
    run("ip6tables", "-P", "OUTPUT", "DROP")
    run("ip6tables", "-F", "OUTPUT")
    run("ip6tables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    # Docker may publish [::]:4545 and forward IPv6 client connections.
    # Do not black-hole those response packets.
    run("ip6tables", "-A", "OUTPUT", "-p", "tcp", "--sport", "4545",
        "-j", "ACCEPT")
    # No custom web "ip rule" is needed. Because our marked TCP packets
    # do not match wg-quick's "not fwmark" VPN rule, the normal Docker
    # main-table default route is used even when wg0 is up.
    # Replies to IPv6 clients stay on the normal IPv6 route; IPv6
    # downloader egress remains denied by ip6tables OUTPUT DROP.
    FIREWALL_ENABLED = True


def allow_endpoint(ip, port):
    run("iptables", "-F", "VPN_ENDPOINT")
    run("iptables", "-A", "VPN_ENDPOINT", "-p", "udp", "-d", ip,
        "--dport", str(port), "-j", "ACCEPT")


def stop_tunnel():
    if run("ip", "link", "show", "wg0", check=False).returncode == 0:
        run("wg-quick", "down", str(ACTIVE_FILE))


def _direct():
    """Exit WireGuard and restore the normal Docker underlay after a job.

    Block all outbound traffic BEFORE tearing down the tunnel. Allow direct
    IPv4/IPv6 only after wg0 has disappeared and its policy routes have gone.
    A failed transition stays fail-closed; never claim direct connectivity.
    """
    global FIREWALL_ENABLED
    data = metadata()
    if not FIREWALL_ENABLED and not data.get("active"):
        return {"direct": True, "mode": "direct"}
    # No direct egress may escape during tunnel teardown.
    try:
        run("iptables", "-P", "OUTPUT", "DROP")
        run("ip6tables", "-P", "OUTPUT", "DROP")
        FIREWALL_ENABLED = True
        stop_tunnel()
        if run("ip", "link", "show", "wg0", check=False).returncode == 0:
            raise RuntimeError("WireGuard interface still active")
        # Only the private gateway ever installed these OUTPUT rules.
        run("iptables", "-F", "OUTPUT")
        run("ip6tables", "-F", "OUTPUT")
        mark_match = ("-p", "tcp", "--sport", str(WEB_PORT),
                      "-j", "MARK", "--set-mark", str(WG_FWMARK))
        run("iptables", "-t", "mangle", "-D", "OUTPUT",
            *mark_match, check=False)
        # Reopen the normal connection last, after WireGuard is gone.
        run("iptables", "-P", "OUTPUT", "ACCEPT")
        run("ip6tables", "-P", "OUTPUT", "ACCEPT")
        data["active"] = None
        save_metadata(data)
        FIREWALL_ENABLED = False
        return {"direct": True, "mode": "direct"}
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        try:
            firewall()
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            pass
        raise VPNConnectionError(
            "direct", "Could not restore the normal connection; outbound "
            "traffic remains blocked. Check the WireGuard gateway."
        ) from None


def direct():
    with LOCK:
        if TASK["running"]:
            raise RuntimeError("VPN profile verification is running")
        return _direct()


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
    try:
        if not FIREWALL_ENABLED:
            firewall()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        raise VPNConnectionError(
            "firewall", "VPN kill-switch rules could not be installed. "
            "Check gateway NET_ADMIN and host firewall support.") from None
    try:
        stop_tunnel()
        allow_endpoint(candidate["endpoint"], candidate["port"])
        write_private(ACTIVE_FILE, candidate["text"])
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        raise VPNConnectionError(
            "preparation", "Gateway could not prepare WireGuard's interface "
            "or permit the Surfshark server endpoint.") from None
    failure = None
    try:
        startup = run("wg-quick", "up", str(ACTIVE_FILE), check=False)
        if startup.returncode:
            raise setup_error(startup.stderr)
        wait_for_handshake()
        check_dns()
        check = probe()
        if not check.get("ip"):
            raise VPNConnectionError("egress", "VPN handshake worked, but "
                                     "there was no verified public IP.")
        data["active"] = profile_id
        save_metadata(data)
        return check
    except VPNConnectionError as exc:
        if exc.phase == "handshake":
            # Capture counters while wg0 and the per-endpoint firewall rule
            # still exist, before rollback tears down the interface.
            exc.diagnostics = safe_handshake_diagnostics(candidate)
        failure = exc
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        failure = VPNConnectionError(
            "setup", "VPN initialization failed before exit-IP verification.")
    # Preserve the safe failure reason while attempting a fail-closed rollback.
    try:
        stop_tunnel()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        pass
    data["active"] = None
    save_metadata(data)
    if previous_text and previous:
        try:
            old = parse_profile(previous_text)
            allow_endpoint(old["endpoint"], old["port"])
            write_private(ACTIVE_FILE, old["text"])
            restored = run("wg-quick", "up", str(ACTIVE_FILE), check=False)
            if not restored.returncode:
                # A restarted interface is not automatically a working
                # connection; require at least the old peer handshake.
                wait_for_handshake(seconds=6)
                data["active"] = previous
                save_metadata(data)
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            pass
    raise failure


def probe():
    """Check the public exit after a verified handshake, with a backup API.

    Both services use HTTPS and report the requester's apparent egress IP.
    Their errors are deliberately not forwarded to the UI.
    """
    hosts = (
        ("https://ipwho.is/", "ipwhois"),
        ("https://ipapi.co/json/", "ipapi"),
    )
    for url, provider in hosts:
        try:
            request = Request(url, headers={
                "User-Agent": "yt-dlp-wireguard-gateway/1.0",
                "Accept": "application/json",
            })
            with urlopen(request, timeout=5) as response:
                if int(response.headers.get("Content-Length", "0") or 0) > 65536:
                    continue
                item = json.loads(response.read(65536))
            ip = str(item.get("ip") or "")
            if not ip or (provider == "ipwhois" and item.get("success") is False):
                continue
            ipaddress.ip_address(ip)
            country = item.get("country") if provider == "ipwhois" else item.get("country_name")
            return {"ip": ip, "country": str(country or "Unknown"),
                    "country_code": str(item.get("country_code") or "")}
        except (HTTPError, URLError, ValueError, TypeError, OSError):
            continue
    raise VPNConnectionError(
        "exit_ip", "WireGuard handshake and DNS succeeded, but both HTTPS "
        "public-IP services failed. Check VPN egress or HTTPS connectivity.")


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


def stored_client_public_key(profile_id):
    """Derive the public key from an EXISTING uploaded profile.

    This also works for profiles uploaded before the public-key UI existed.
    A stat-based cache keeps normal status polling cheap and stores only
    public keys, never configuration contents or client private keys.
    """
    if not isinstance(profile_id, str) or not re.fullmatch(r"[0-9a-f]{32}", profile_id):
        return None
    file = ROOT / (profile_id + ".conf")
    try:
        stat = file.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        cached = PROFILE_PUBLIC_CACHE.get(profile_id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        value = client_public_key(file.read_text())
    except (OSError, UnicodeError):
        return None
    if value is not None:
        PROFILE_PUBLIC_CACHE[profile_id] = (fingerprint, value)
    return value


def list_profiles():
    # Metadata writes are atomic file replacements, and the public-key
    # cache avoids repeated wg subprocesses during status polling.
    value = metadata()
    profiles = [
        {**item, "client_public_key": stored_client_public_key(item.get("id"))}
        for item in value["profiles"]
    ]
    status = {"running": TASK["running"], "done": TASK["done"],
              "message": TASK["message"], "results": list(TASK["results"])}
    connected = FIREWALL_ENABLED and bool(value["active"]) and has_handshake()
    mode = "vpn" if connected else "blocked" if FIREWALL_ENABLED else "direct"
    return {"profiles": profiles, "active": value["active"] if connected else None,
            "mode": mode, "default_mode": "direct",
            "kill_switch": FIREWALL_ENABLED,
            "tunnel_ready": connected,
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
        PROFILE_PUBLIC_CACHE.pop(profile_id, None)
        return {"deleted": True}


def rotate():
    """Try alternate profiles in order until a different exit IP is verified.

    A failed tunnel or same-IP endpoint is never reported as successful.
    Every candidate is attempted at most once; MAX_PROFILES bounds work.
    """
    with LOCK:
        if TASK["running"]:
            raise RuntimeError("VPN rotation test is running")
        data = metadata()
        choices = data["profiles"]
        if not choices:
            raise ValueError("Upload a WireGuard profile first")
        previous = data.get("active")
        if previous and len(choices) < 2:
            raise ValueError("Upload at least two WireGuard profiles for automatic rotation")
        current = next((i for i, p in enumerate(choices)
                        if p["id"] == previous), -1)
        order = choices[current + 1:] + choices[:current + 1]
        candidates = [item for item in order if item["id"] != previous]
        # A direct-origin recovery must change the public IP as well.
        old_ip = probe()["ip"]
        for candidate in candidates:
            try:
                result = _activate(candidate["id"])
            except (VPNConnectionError, RuntimeError, OSError, subprocess.TimeoutExpired):
                continue
            if old_ip is None or result["ip"] != old_ip:
                return {"profile": candidate, **result,
                        "changed_profile": previous != candidate["id"],
                        "changed_ip": True}
        if previous and metadata().get("active") != previous:
            try:
                _activate(previous)
            except (VPNConnectionError, RuntimeError, OSError, subprocess.TimeoutExpired):
                raise VPNConnectionError(
                    "restore", "No alternate VPN exit IP was verified and the previous "
                    "tunnel could not be restored; downloads remain fail-closed.") from None
        if not previous:
            # Failed VPN recovery must not strand the next normal job behind
            # the kill switch or leave an unverified profile running.
            _direct()
        raise VPNConnectionError(
            "exit_ip", "No other WireGuard profile produced a different verified exit IP")


def test_worker():
    with LOCK:
        choices = metadata()["profiles"]
    previous_ip = None
    try:
        for entry in choices:
            try:
                with LOCK:
                    result = _activate(entry["id"])
                    changed_ip = previous_ip is None or previous_ip != result["ip"]
                    expected = expected_country_code(entry["country"])
                    observed = result.get("country_code") or ""
                    country_match = expected is None or expected == observed
                    ok = changed_ip and bool(observed) and country_match
                    if not changed_ip:
                        note = "Exit IP did not change"
                    elif not observed:
                        note = "Public IP found, but location lookup did not identify a country"
                    elif expected is None:
                        note = "Public IP and country observed; source filename has no recognizable country code"
                    elif not country_match:
                        note = "Expected " + expected + "; observed " + observed
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
                                            "phase": exc.phase if isinstance(exc, VPNConnectionError) else "unknown",
                                            "diagnostics": getattr(exc, "diagnostics", {}),
                                            "message": str(exc) if isinstance(exc, VPNConnectionError)
                                            else "VPN connection failed during setup."})
    except Exception:
        with LOCK:
            TASK["message"] = "VPN profile verification failed"
    finally:
        with LOCK:
            # Profile testing must ALWAYS return to the direct default,
            # including on a failed test or a user-triggered exception.
            try:
                _direct()
            except VPNConnectionError:
                TASK["message"] = "Could not restore direct connection; outbound blocked"
            else:
                TASK["message"] = "Profile verification finished; direct mode restored"
            TASK["running"] = False
            TASK["done"] = True


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
                elif self.path == "/direct":
                    result = direct()
                elif self.path == "/test":
                    result = start_test()
                elif re.fullmatch(r"/activate/[0-9a-f]{32}", self.path):
                    result = activate(self.path.split("/")[-1])
                elif re.fullmatch(r"/delete/[0-9a-f]{32}", self.path):
                    result = delete_profile(self.path.split("/")[-1])
                else:
                    return self.reply(404, {"error": "Not found"})
                return self.reply(200, {"ok": True, **result})
            except VPNConnectionError as exc:
                return self.reply(400, {
                    "error": str(exc), "phase": exc.phase,
                    "diagnostics": getattr(exc, "diagnostics", {}),
                })
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
    owner = TOKEN_FILE.stat()
    if owner.st_uid != uid or owner.st_gid != gid:
        os.chown(TOKEN_FILE, uid, gid)
    TOKEN = token


def main():
    ensure_control_token()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    # Direct is the only automatic startup mode, even when profiles exist.
    # Never reconnect an old profile solely because it was selected before
    # the gateway was recreated. Fail closed if stale rules cannot be removed.
    try:
        with LOCK:
            firewall()
            _direct()
    except (VPNConnectionError, RuntimeError, OSError, subprocess.TimeoutExpired):
        try:
            firewall()
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            pass
    ThreadingHTTPServer(("127.0.0.1", 8787), Handler).serve_forever()


if __name__ == "__main__":
    main()
