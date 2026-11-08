"""Offline upload and native interface regressions."""
import io
import stat
import threading
from pathlib import Path

import pytest
from flask import Flask, render_template_string

import youtube_cookies


def sample(domain=".youtube.com"):
    return ("# Netscape HTTP Cookie File\n"
            + domain + "\tTRUE\t/\tTRUE\t0\tSAPISID\tprivate-sample\n").encode()


def test_filter_only_youtube_entries():
    raw = (b"# HTTP Cookie File\r\n"
           b"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-youtube\r\n"
           b".google.com\tTRUE\t/\tTRUE\t0\tGMAIL_AT\tother-site-secret\r\n")
    cleaned = youtube_cookies.filter_youtube_cookies(raw)
    assert "#HttpOnly_.youtube.com" in cleaned
    assert "other-site-secret" not in cleaned
    assert ".google.com" not in cleaned


@pytest.mark.parametrize("payload", [
    b"", b"not-netscape", b"# Netscape HTTP Cookie File\n",
    b"# HTTP Cookie File\ninvalid\n", sample(".notyoutube.com"),
    b"a" * (youtube_cookies.MAX_BYTES + 1),
])
def test_invalid_or_unrelated_files_rejected(payload):
    with pytest.raises(youtube_cookies.CookieFileError):
        youtube_cookies.filter_youtube_cookies(payload)


def test_atomic_private_write(tmp_path):
    destination = tmp_path / "cookies.txt"
    youtube_cookies.write_private(destination, "first\n")
    youtube_cookies.write_private(destination, "second\n")
    assert destination.read_text() == "second\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert sorted(tmp_path.iterdir()) == [destination]


def test_default_and_existing_cookie_path(tmp_path):
    assert youtube_cookies.cookie_path(tmp_path) == tmp_path / "cookies.txt"
    assert youtube_cookies.cookie_path(tmp_path, "/existing/cookies.txt") == Path("/existing/cookies.txt")


@pytest.fixture
def server(tmp_path):
    app = Flask(__name__)
    jobs = {}
    destination = tmp_path / "cookies.txt"
    youtube_cookies.register(app, jobs, threading.Lock(), destination)

    @app.get("/")
    def index():
        return render_template_string("{{ youtube_cookie_token }}")

    with app.test_client() as client:
        token = client.get("/").get_data(as_text=True)
        yield client, jobs, destination, token


def test_upload_remove_and_csrf(server):
    client, jobs, destination, token = server
    assert client.post("/youtube/cookies/upload", data={
        "_cookie_token": "wrong", "cookies": (io.BytesIO(sample()), "cookies.txt"),
    }).status_code == 403
    response = client.post("/youtube/cookies/upload", data={
        "_cookie_token": token, "cookies": (io.BytesIO(sample()), "cookies.txt"),
    }, content_type="multipart/form-data")
    assert response.status_code == 303
    assert "cookies=saved" in response.headers["Location"]
    assert destination.exists()
    assert "private-sample" not in response.get_data(as_text=True)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    removed = client.post("/youtube/cookies/remove", data={"_cookie_token": token})
    assert "cookies=removed" in removed.headers["Location"]
    assert not destination.exists()


def test_busy_jobs_protect_cookie_file(server):
    client, jobs, destination, token = server
    jobs["a"] = {"status": "queued"}
    response = client.post("/youtube/cookies/upload", data={
        "_cookie_token": token, "cookies": (io.BytesIO(sample()), "cookies.txt"),
    }, content_type="multipart/form-data")
    assert "cookies=busy" in response.headers["Location"]
    assert not destination.exists()


def test_invalid_upload_preserves_existing_file(server):
    client, jobs, destination, token = server
    youtube_cookies.write_private(destination, "existing\n")
    response = client.post("/youtube/cookies/upload", data={
        "_cookie_token": token, "cookies": (io.BytesIO(b"bad"), "cookies.txt"),
    }, content_type="multipart/form-data")
    assert "cookies=format" in response.headers["Location"]
    assert destination.read_text() == "existing\n"


def test_cookie_file_is_wired_to_all_download_paths():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app.py").read_text()
    assert 'COOKIES_FILE = str(cookie_path(STATE_DIR, os.getenv("COOKIES_FILE", "")))' in source
    assert '"--cookies", COOKIES_FILE' in source
    assert source.count('"--cookie-file", COOKIES_FILE') == 2
    html = (root / "templates/index.html").read_text()
    assert 'id="youtube-cookie-upload-form"' in html
    assert 'url_for(\'remove_youtube_cookies\')' in html
