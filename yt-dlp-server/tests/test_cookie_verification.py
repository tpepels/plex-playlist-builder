"""Offline checks for YouTube authentication and metadata-only verification."""
import io
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, render_template_string

import youtube_cookies as yc


def cookie_file(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tsecret-value\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tsecret-sid\n"
    )
    return path


def test_yt_dlp_metadata_check_succeeds_only_with_account_login(tmp_path, monkeypatch):
    path = cookie_file(tmp_path)
    monkeypatch.setattr(yc, "youtube_login_state", lambda path: True)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, yc.CHECK_VIDEO_ID + "\n", "")
    monkeypatch.setattr(yc.subprocess, "run", run)
    args = lambda url: ["--extractor-args", "youtube:player_client=mweb"]
    result = yc.verify_youtube_cookies(path, args)
    assert result["status"] == "valid"
    command, kw = calls[0]
    assert "--simulate" in command
    assert "--skip-download" in command
    assert "--cookies" in command
    assert "--no-playlist" in command
    assert command[-1] == yc.CHECK_VIDEO_URL
    assert "youtube:player_client=mweb" in command
    assert kw["timeout"] == 30
    assert path.read_text().count("secret-sid") == 1
    assert not list(tmp_path.glob(".youtube-verify-*"))
    assert "secret" not in str(result)


def test_public_metadata_is_not_enough_without_login(tmp_path, monkeypatch):
    path = cookie_file(tmp_path)
    monkeypatch.setattr(yc, "youtube_login_state", lambda path: False)
    monkeypatch.setattr(yc.subprocess, "run",
                        lambda *a, **kw: pytest.fail("Public video must not be probed"))
    assert yc.verify_youtube_cookies(path)["status"] == "rejected"
    monkeypatch.setattr(yc, "youtube_login_state", lambda path: None)
    assert yc.verify_youtube_cookies(path)["status"] == "inconclusive"


@pytest.mark.parametrize("error", [
    TimeoutError("timeout"), OSError("network"),
    yc.http.cookiejar.LoadError("invalid cookies"),
])
def test_network_or_cookie_loading_errors_are_inconclusive(tmp_path, monkeypatch, error):
    path = cookie_file(tmp_path)
    monkeypatch.setattr(yc, "youtube_login_state",
                        lambda p: (_ for _ in ()).throw(error))
    assert yc.verify_youtube_cookies(path)["status"] == "inconclusive"


@pytest.mark.parametrize("outcome,expected", [
    (subprocess.CompletedProcess([], 1, "", "Sign in to confirm you're not a bot"), "inconclusive"),
    (subprocess.CompletedProcess([], 1, "", "HTTP Error 403"), "inconclusive"),
    (subprocess.CompletedProcess([], 1, "", "Please sign in"), "rejected"),
    (subprocess.CompletedProcess([], 1, "", "Other extractor problem"), "inconclusive"),
    (subprocess.CompletedProcess([], 0, "other video\n", ""), "inconclusive"),
])
def test_extract_failures_are_sanitized(tmp_path, monkeypatch, outcome, expected):
    path = cookie_file(tmp_path)
    monkeypatch.setattr(yc, "youtube_login_state", lambda p: True)
    monkeypatch.setattr(yc.subprocess, "run", lambda *a, **kw: outcome)
    result = yc.verify_youtube_cookies(path)
    assert result["status"] == expected
    assert "secret" not in str(result)
    assert not list(tmp_path.glob(".youtube-verify-*"))


def test_probe_timeout_is_inconclusive_and_cleans_up(tmp_path, monkeypatch):
    path = cookie_file(tmp_path)
    monkeypatch.setattr(yc, "youtube_login_state", lambda p: True)
    monkeypatch.setattr(yc.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            subprocess.TimeoutExpired("yt-dlp", 30)))
    assert yc.verify_youtube_cookies(path)["status"] == "inconclusive"
    assert not list(tmp_path.glob(".youtube-verify-*"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = Flask(__name__)
    jobs = {}
    path = cookie_file(tmp_path)
    yc.register(app, jobs, threading.Lock(), path)

    @app.get("/")
    def index():
        return render_template_string("{{ youtube_cookie_token }}")
    with app.test_client() as c:
        token = c.get("/").get_data(as_text=True)
        yield c, token, jobs, path


def test_verification_endpoint_uses_csrf_and_does_not_change_jobs(client, monkeypatch):
    c, token, jobs, path = client
    monkeypatch.setattr(yc, "verify_youtube_cookies",
                        lambda p, extractor_args: {"status": "valid", "message": "Session recognized"})
    assert c.post("/youtube/cookies/verify", data={"_cookie_token": "invalid"}).status_code == 403
    response = c.post("/youtube/cookies/verify", data={"_cookie_token": token})
    assert response.status_code == 200 and response.json["status"] == "valid"
    assert jobs == {}


def test_verification_rejects_active_jobs(client, monkeypatch):
    c, token, jobs, path = client
    jobs["in-progress"] = {"status": "running"}
    monkeypatch.setattr(yc, "verify_youtube_cookies",
                        lambda *a: pytest.fail("Must not verify during download"))
    response = c.post("/youtube/cookies/verify", data={"_cookie_token": token})
    assert response.status_code == 409
    assert response.json["status"] == "inconclusive"


@pytest.mark.parametrize("html,expected", [
    (b'<script>ytcfg.set({"LOGGED_IN":true,"HL":"en"});</script>', True),
    (b'<script>ytcfg.set({"LOGGED_IN":false});</script>', False),
    (b'<html>Unrecognizable consent response</html>', None),
    (b'<script>{"LOGGED_IN":true,"LOGGED_IN":false}</script>', None),
])
def test_login_recognition_uses_youtube_page_not_cookie_presence(
    tmp_path, monkeypatch, html, expected,
):
    path = cookie_file(tmp_path)
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def geturl(self):
            return yc.LOGIN_PAGE_URL
        def read(self, limit):
            assert limit == 2_000_000
            return html

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == yc.LOGIN_PAGE_URL
            assert timeout == 12
            return Response()

    monkeypatch.setattr(yc, "build_opener", lambda *args: Opener())
    assert yc.youtube_login_state(path) is expected


def test_account_redirect_to_google_is_rejected(tmp_path, monkeypatch):
    path = cookie_file(tmp_path)
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def geturl(self):
            return "https://accounts.google.com/ServiceLogin"
        def read(self, limit):
            pytest.fail("Do not inspect cross-origin sign-in form")
    class Opener:
        def open(self, request, timeout):
            return Response()
    monkeypatch.setattr(yc, "build_opener", lambda *args: Opener())
    assert yc.youtube_login_state(path) is False
