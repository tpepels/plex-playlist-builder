"""Supervise the privileged VPN controller and unprivileged music server.

One Docker container, two OS processes in the same network namespace:
- vpn_gateway.py runs as root with NET_ADMIN, owns wg0 and iptables.
- gunicorn runs via gosu as the configured non-root UID/GID.

If either service exits, terminate the other and exit non-zero so Docker
restarts the entire container. Never restart only the web process behind
a stale WireGuard route. The gateway must be ready before the web process
can resume the download queue.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 8787
BIND_PORT = 4545
READY_TIMEOUT = 30
STOP_TIMEOUT = 10
HEALTH_INTERVAL = 10
HEALTH_STARTUP_GRACE = 15
HEALTH_FAILURE_LIMIT = 3


def web_health():
    """Read a bounded local health report, including on HTTP 503.

    Persistent state faults are operator-recoverable. Do not restart in a
    loop and hide the dashboard while the owner repairs a damaged volume.
    """
    try:
        with urlopen(f"http://127.0.0.1:{BIND_PORT}/health", timeout=2) as reply:
            value = json.load(reply)
    except HTTPError as exc:
        try:
            value = json.loads(exc.read(16384))
        except (ValueError, OSError, UnicodeError):
            return None
    except (OSError, URLError, TimeoutError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def evaluate_web_health(value, failures):
    """Consecutive failed checks restart a dead worker or unresponsive UI."""
    if isinstance(value, dict) and value.get("state_error"):
        # The UI remains reachable for diagnosis; a restart cannot repair
        # corrupt backups or disk-full conditions.
        return 0, False
    if isinstance(value, dict) and value.get("worker_alive") is True:
        return 0, False
    failures += 1
    return failures, failures >= HEALTH_FAILURE_LIMIT


def runtime_ids(environment):
    """Only an explicit, positive non-root identity may run the downloader."""
    try:
        uid = int(environment.get("VPN_DOWNLOADER_UID", "1000"))
        gid = int(environment.get("VPN_DOWNLOADER_GID", "1000"))
    except (TypeError, ValueError):
        raise ValueError("Downloader UID and GID must be numeric") from None
    if not 1 <= uid <= 65534 or not 1 <= gid <= 65534:
        raise ValueError("Downloader must use a non-root UID/GID (1..65534)")
    return uid, gid


def gateway_ready(timeout=READY_TIMEOUT, *, process=None):
    """A bound gateway socket means firewall initialization has finished."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("WireGuard controller exited before becoming ready")
        try:
            with socket.create_connection((GATEWAY_HOST, GATEWAY_PORT), timeout=0.3):
                return
        except (OSError, TimeoutError):
            time.sleep(0.1)
    raise RuntimeError("WireGuard controller did not become ready")


def stop_processes(processes, grace=STOP_TIMEOUT):
    """Terminate both process groups; don't strand children on supervisor exit."""
    for child in processes:
        if child.poll() is not None:
            continue
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    for child in processes:
        if child.poll() is not None:
            continue
        try:
            child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def supervise():
    if os.geteuid() != 0:
        raise RuntimeError("The container supervisor requires root; gunicorn is dropped to a non-root UID")
    uid, gid = runtime_ids(os.environ)
    if not os.path.exists("/dev/net/tun"):
        raise RuntimeError("Missing /dev/net/tun; configure the Docker device mapping")

    children = []
    old_handlers = {}
    terminated = False

    def shutdown(_signum, _frame):
        nonlocal terminated
        terminated = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        old_handlers[sig] = signal.signal(sig, shutdown)

    try:
        gateway = subprocess.Popen(
            [sys.executable, "-u", "/app/vpn_gateway.py"],
            start_new_session=True,
        )
        children.append(gateway)
        gateway_ready(process=gateway)
        if terminated:
            return 0

        # Never run yt-dlp, spotDL, Flask or Gunicorn with NET_ADMIN.
        downloader = subprocess.Popen([
            # Drop NET_ADMIN from the bounding set and forbid privilege
            # escalation by executable file capabilities or setuid binaries.
            "setpriv", "--bounding-set=-net_admin", "--no-new-privs",
            "gosu", f"{uid}:{gid}",
            "gunicorn", "--workers", "1", "--threads", "4",
            "--bind", f"0.0.0.0:{BIND_PORT}",
            "--access-logfile", "-", "--error-logfile", "-", "app:app",
        ], start_new_session=True)
        children.append(downloader)
        next_health = time.monotonic() + HEALTH_STARTUP_GRACE
        health_failures = 0

        while not terminated:
            if gateway.poll() is not None:
                print("ERROR: privileged WireGuard controller exited", flush=True)
                return 1
            if downloader.poll() is not None:
                print("ERROR: downloader web server exited", flush=True)
                return 1
            if time.monotonic() >= next_health:
                health_failures, broken = evaluate_web_health(
                    web_health(), health_failures,
                )
                next_health = time.monotonic() + HEALTH_INTERVAL
                if broken:
                    print(
                        "ERROR: downloader worker or local web service is "
                        "unresponsive; restarting the container to recover "
                        "the durable queue.",
                        flush=True,
                    )
                    return 1
            time.sleep(0.2)
        return 0
    finally:
        stop_processes(children)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def main():
    try:
        return supervise()
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: container startup: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
