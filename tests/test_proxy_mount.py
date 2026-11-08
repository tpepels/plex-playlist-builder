"""Full downloader UI must work at the Playlist Manager reverse-proxy prefix."""
import re
from pathlib import Path

import app as server


PREFIX = "/downloads/engine"
FORWARDED = {
    "X-Forwarded-Prefix": PREFIX,
    "X-Forwarded-Host": "media-server:5051",
    "X-Forwarded-Proto": "http",
}


def test_proxied_ui_forms_assets_and_actions_use_playlist_manager_origin():
    with server.app.test_client() as client:
        response = client.get("/", headers=FORWARDED)
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert '<meta name="downloader-base" content="/downloads/engine">' in html
        assert f'action="{PREFIX}/download"' in html
        assert f'action="{PREFIX}/youtube/cookies/upload"' in html
        assert f'action="{PREFIX}/youtube/cookies/remove"' in html
        assert f'href="{PREFIX}/chrome-extension.zip"' in html
        assert f'{PREFIX}/static/vpn-manager.js' in html
        assert f'{PREFIX}/static/youtube-cookies.js' in html
        assert 'downloaderFetch("/api/jobs"' in html
        assert 'downloaderPath("/?job=")' in html


def test_unproxied_web_interface_is_unchanged():
    with server.app.test_client() as client:
        response = client.get("/")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert '<meta name="downloader-base" content="">' in html
        assert 'action="/download"' in html
        assert 'action="/youtube/cookies/upload"' in html
        assert 'src="/static/vpn-manager.js' in html


def test_cookie_origin_validation_follows_forwarded_manager_origin():
    with server.app.test_client() as client:
        html = client.get("/", headers=FORWARDED).get_data(as_text=True)
        token = re.search(
            r'name="_cookie_token" value="([^"]+)"', html,
        ).group(1)
        response = client.post("/youtube/cookies/verify", headers={
            **FORWARDED,
            "Origin": "http://media-server:5051",
        }, data={"_cookie_token": token})
        assert response.status_code in {200, 409}
        other_origin = client.post("/youtube/cookies/verify", headers={
            **FORWARDED,
            "Origin": "http://other-host:5051",
        }, data={"_cookie_token": token})
        assert other_origin.status_code == 403


def test_all_static_fetches_use_dynamic_base():
    root = Path(__file__).resolve().parents[1]
    html = (root / "templates" / "index.html").read_text()
    vpn = (root / "static" / "vpn-manager.js").read_text()
    cookies = (root / "static" / "youtube-cookies.js").read_text()
    assert 'fetch("/api/' not in html
    assert 'fetch("/api/vpn"' not in vpn
    assert 'fetch("/youtube/cookies' not in cookies
    assert 'downloader-base' in html and 'downloader-base' in vpn
    assert 'downloader-base' in cookies
