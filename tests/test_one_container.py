"""Single-container deployment and supervisor regressions (no Docker daemon)."""
from pathlib import Path
from types import SimpleNamespace
import signal

import pytest
import container_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


def test_canonical_compose_exposes_only_the_downloader_service():
    compose = (ROOT / "compose.yaml").read_text()
    overlay = (ROOT / "compose.parent-vpn.yaml").read_text()
    for source in (compose, overlay):
        assert 'user: "0:0"' in source
        assert 'VPN_GATEWAY_ENABLED: "1"' in source
        assert 'VPN_PROFILE_DIR: /profiles' in source
        assert 'VPN_DOWNLOADER_UID:' in source
        assert 'VPN_DOWNLOADER_GID:' in source
        assert 'net.ipv4.conf.all.src_valid_mark: "1"' in source
        assert "NET_ADMIN" in source
        assert "/dev/net/tun:/dev/net/tun" in source
        assert "/srv/yt-dlp-server/wireguard-profiles:/profiles" in source
        assert "wireguard-gateway:" not in source
        assert "network_mode:" not in source
        assert "depends_on:" not in source
    assert 'ports:' in compose and '"4545:4545"' in compose
    assert "ports:" not in overlay  # parent retains its existing mapping
    assert "services:" in compose
    assert compose.count("  yt-dlp-server:") == 1
    assert not (ROOT / "Dockerfile.vpn-gateway").exists()
    assert not (ROOT / "compose.wireguard.yaml").exists()
    assert not (ROOT / "compose.surfshark.yaml").exists()


def test_single_image_contains_privileged_toolchain_and_nonroot_launcher():
    docker = (ROOT / "Dockerfile").read_text()
    for name in ("iptables", "iproute2", "wireguard-tools", "gosu", "util-linux"):
        assert name in docker
    assert "COPY app.py" in docker
    assert "vpn_gateway.py" in docker
    assert "container_runtime.py ./" in docker
    assert 'CMD ["python", "-u", "/app/container_runtime.py"]' in docker
    assert "8787" in docker and "4545" in docker
    assert "gosu" not in docker.split("CMD [", 1)[1]  # supervisor owns privilege drop
    assert "Dockerfile.vpn-gateway" not in docker


@pytest.mark.parametrize("env", [
    {"VPN_DOWNLOADER_UID": "0", "VPN_DOWNLOADER_GID": "1000"},
    {"VPN_DOWNLOADER_UID": "1000", "VPN_DOWNLOADER_GID": "0"},
    {"VPN_DOWNLOADER_UID": "-1", "VPN_DOWNLOADER_GID": "1000"},
    {"VPN_DOWNLOADER_UID": "bad", "VPN_DOWNLOADER_GID": "1000"},
    {"VPN_DOWNLOADER_UID": "999999", "VPN_DOWNLOADER_GID": "1000"},
])
def test_download_worker_cannot_be_launched_as_root(env):
    with pytest.raises(ValueError):
        runtime.runtime_ids(env)


def test_valid_arbitrary_unprivileged_uid_gid_supported():
    assert runtime.runtime_ids({}) == (1000, 1000)
    assert runtime.runtime_ids({
        "VPN_DOWNLOADER_UID": "1201", "VPN_DOWNLOADER_GID": "2002"
    }) == (1201, 2002)


def test_gateway_must_be_ready_before_unprivileged_gunicorn(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime.os.path, "exists", lambda path: path == "/dev/net/tun")
    monkeypatch.setenv("VPN_DOWNLOADER_UID", "1000")
    monkeypatch.setenv("VPN_DOWNLOADER_GID", "1000")

    class Child:
        def __init__(self, command, index):
            self.command = command
            self.index = index
            self.pid = 100 + index
        def poll(self):
            if self.index == 1:
                return 1
            return None

    def popen(command, **options):
        calls.append(("spawn", command))
        assert options["start_new_session"] is True
        return Child(command, len([x for x in calls if x[0] == "spawn"]) - 1)

    def ready(*, process):
        assert process.command == [
            runtime.sys.executable, "-u", "/app/vpn_gateway.py"
        ]
        calls.append(("ready", process.pid))

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(runtime, "gateway_ready", ready)
    monkeypatch.setattr(runtime, "stop_processes",
                        lambda children: calls.append(("stop", len(children))))
    monkeypatch.setattr(runtime.signal, "signal",
                        lambda *args: signal.SIG_DFL)

    assert runtime.supervise() == 1
    assert [item[0] for item in calls] == ["spawn", "ready", "spawn", "stop"]
    web = calls[2][1]
    assert web[:3] == [
        "setpriv", "--bounding-set=-net_admin", "--no-new-privs"
    ]
    assert web[3:5] == ["gosu", "1000:1000"]
    assert "gunicorn" in web
    assert "--bind" in web and "0.0.0.0:4545" in web
    assert all("sudo" not in word for word in web)


def test_gateway_failure_terminates_downloader(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime.os.path, "exists", lambda path: True)

    class Child:
        def __init__(self, which):
            self.pid = 1 + which
            self.which = which
        def poll(self):
            return 42 if self.which == 0 else None

    children = []
    def popen(*args, **kwargs):
        child = Child(len(children))
        children.append(child)
        return child

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(runtime, "gateway_ready", lambda *, process: None)
    monkeypatch.setattr(runtime, "stop_processes",
                        lambda procs: calls.append(tuple(procs)))
    monkeypatch.setattr(runtime.signal, "signal", lambda *args: signal.SIG_DFL)

    assert runtime.supervise() == 1
    assert calls == [tuple(children)]


def test_early_gateway_failure_never_starts_web(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime.os.path, "exists", lambda path: True)
    fake = SimpleNamespace(pid=300, poll=lambda: 1)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: (
        calls.append(a) or fake
    ))
    def not_ready(*, process):
        assert process is fake
        raise RuntimeError("gateway failed")
    monkeypatch.setattr(runtime, "gateway_ready", not_ready)
    monkeypatch.setattr(runtime, "stop_processes", lambda children: None)
    monkeypatch.setattr(runtime.signal, "signal", lambda *args: signal.SIG_DFL)
    with pytest.raises(RuntimeError, match="gateway failed"):
        runtime.supervise()
    assert len(calls) == 1


def test_signal_before_web_start_stops_gateway_and_skips_server(monkeypatch):
    calls = []
    handlers = {}
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime.os.path, "exists", lambda path: True)
    child = SimpleNamespace(pid=8, poll=lambda: None)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: (
        calls.append("gateway") or child
    ))
    def signal_handler(sig, fn):
        handlers[sig] = fn
        return signal.SIG_DFL
    monkeypatch.setattr(runtime.signal, "signal", signal_handler)
    monkeypatch.setattr(runtime, "gateway_ready",
                        lambda *, process: handlers[signal.SIGTERM](signal.SIGTERM, None))
    monkeypatch.setattr(runtime, "stop_processes",
                        lambda children: calls.append("stop"))
    assert runtime.supervise() == 0
    assert calls == ["gateway", "stop"]


def test_stop_processes_terminates_both_process_groups(monkeypatch):
    calls = []
    class Child:
        def __init__(self, pid):
            self.pid = pid
            self.done = False
        def poll(self):
            return 0 if self.done else None
        def wait(self, timeout=None):
            self.done = True
            calls.append(("wait", self.pid))
    monkeypatch.setattr(runtime.os, "killpg",
                        lambda pid, sig: calls.append(("signal", pid, sig)))
    runtime.stop_processes([Child(10), Child(11)])
    assert calls[:2] == [
        ("signal", 10, signal.SIGTERM),
        ("signal", 11, signal.SIGTERM),
    ]
    assert len([a for a in calls if a[0] == "wait"]) == 2


def test_migration_docs_preserve_state_and_remove_orphan_container():
    docs = (ROOT / "README.md").read_text()
    assert "docker-compose.override.yml" in docs
    assert "docker rm -f yt-dlp-wireguard-gateway" in docs
    assert "docker compose up -d --build --force-recreate yt-dlp-server" in docs
    assert "docker compose down -v" in docs  # explicitly prohibited
    assert "docker compose exec -u 1000:1000" in docs
    assert "docker volume prune" in docs  # explicitly prohibited
