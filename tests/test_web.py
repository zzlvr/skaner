import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web.config import LANGS, Settings
from web.main import create_app, safe_next

LOCALES = Path(__file__).parent.parent / "web" / "locales"


@pytest.fixture
def client():
    settings = Settings(db_url="sqlite://", secret="x" * 40, base_url="http://test", dev=True)
    return TestClient(create_app(settings))


def csrf(client) -> str:
    """Forms need the CSRF token from a rendered page (and the sk_csrf cookie it sets)."""
    return re.search(r'name="csrf_token" value="([0-9a-f]{64})"', client.get("/").text).group(1)


def test_index_all_languages(client):
    for lang in LANGS:
        r = client.get("/", headers={"accept-language": lang})
        assert r.status_code == 200
        assert f'<html lang="{lang}">' in r.text


def test_locales_have_same_keys():
    keys = {lang: set(json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))) for lang in LANGS}
    for lang in LANGS:
        assert keys[lang] == keys["ru"], f"{lang}: {keys[lang] ^ keys['ru']}"


def test_security_headers(client):
    h = client.get("/").headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert "script-src 'self'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"


def test_no_api_docs(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_language_switch_sets_cookie_and_redirects(client):
    r = client.post("/lang", data={"lang": "kk", "next": "/login", "csrf_token": csrf(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "lang=kk" in r.headers["set-cookie"]
    assert "httponly" in r.headers["set-cookie"].lower()


def test_language_switch_rejects_unknown_language(client):
    r = client.post("/lang", data={"lang": "../../x", "csrf_token": csrf(client)}, follow_redirects=False)
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize(
    "bad", ["//evil.com", "https://evil.com", "/\\evil.com", "evil.com", "/a\r\nb", "/\t/evil.com", "/ /x", "/é", ""]
)
def test_open_redirect_blocked(bad):
    assert safe_next(bad) == "/"


def test_open_redirect_blocked_via_endpoint(client):
    r = client.post("/lang", data={"lang": "ru", "next": "//evil.com", "csrf_token": csrf(client)}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_stub_pages_exist(client):
    for path in ("/privacy", "/terms", "/security", "/contacts", "/bot", "/login", "/healthz"):
        assert client.get(path).status_code == 200


def test_login_escapes_email(client):
    r = client.post("/login", data={"email": '"><script>alert(1)</script>', "csrf_token": csrf(client)})
    assert r.status_code == 400
    assert "<script>alert(1)</script>" not in r.text


def test_static_files_served(client):
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/fonts.css").status_code == 200
    assert client.get("/static/fonts/inter-cyrillic.woff2").status_code == 200


def test_secret_required_outside_dev(monkeypatch):
    monkeypatch.delenv("WEB_SECRET", raising=False)
    monkeypatch.delenv("WEB_DEV", raising=False)
    with pytest.raises(RuntimeError):
        Settings.load()


def test_language_menu_lists_three_languages_and_no_dark_theme(client):
    html = client.get("/").text
    for name in ("Русский", "Қазақша", "English"):
        assert name in html
    assert 'class="langmenu"' in html
    css = client.get("/static/app.css").text
    assert "prefers-color-scheme" not in css


def test_motion_is_safe(client):
    css = client.get("/static/anim.css").text
    assert "prefers-reduced-motion" in css
    for banned in ("blur(", "filter:blur", "<video", "webgl"):
        assert banned not in css.lower()
    html = client.get("/").text
    assert "/static/anim.css" in html and "/static/app.js" in html
    assert "<script>" not in html  # strict CSP: scripts only from our own files


def test_demo_animation_hooks_present(client):
    html = client.get("/").text
    css = client.get("/static/demo.css").text
    for hook in ("tr1", "tr4", "chk1", "chk3", "bx1", "bxw", "exp", "toast", "drag", "cur", "skel", "s1", "s2"):
        assert f'{hook}' in html
    for name in ("@keyframes s2", "@keyframes tr4", "@keyframes curmove", "@keyframes toast"):
        assert name in css


def test_demo_css_has_no_heavy_effects(client):
    css = client.get("/static/demo.css").text.lower()
    assert "blur(" not in css and "filter" not in css


def test_stylesheets_and_scripts_are_revalidated(client):
    for path in ("/static/app.css", "/static/legal.css", "/static/app.js"):
        assert client.get(path).headers["cache-control"] == "no-cache"
