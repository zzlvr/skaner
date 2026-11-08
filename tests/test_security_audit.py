"""Security audit (2026-10-22): attack-style tests. Each block either proves a fix made in this audit (the test failed
before) or locks in a protection that already worked. Fake backend, fake mailer, in-memory database: no network,
no money, no real mail or Telegram.

Fixed in this audit (tests marked FIX):
- /admin/, /internal/..., /app/... with a trailing slash answered 307 (the redirect showed that /admin exists);
- /internal/phone-verified answered 405 to GET even with phone confirmation off (the route was visible);
- POST /contacts had no body limit (a large form, or files Starlette spools to disk); a big POST to /admin answered
  413 while an unknown path answered 404 (an oracle);
- the internal endpoint trusted only the proxy rule: now also loopback-only outside dev;
- WEB_DEV=1 could be started with a public WEB_BASE_URL (no Secure cookies, no HSTS, built-in secret, demo login);
- pages under /join/<token> sent the token in the Referer to our own static files (proxy logs keep Referers);
- /contacts could be cached (no Cache-Control);
- a text PDF with colspan="²" (from a hostile document) could never be downloaded (ValueError);
- Pillow parsed images with every decoder it has; now only the sniffed type's decoder runs.
"""

import asyncio
import io
import json
import re
import sqlite3
from datetime import timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import GifImagePlugin, Image
from sqlalchemy import select

from docstruc.core import files
from docstruc.digitize import sanitize_html
from test_app import SECRET, Clock, FakeBackend, FakeMailer, batch_url, csrf, pdf, png, upload
from test_app import Env as AppEnv
from test_company import Env as CoEnv
from test_company import create, join, link_token
from test_plans_admin import ADMIN, RecordingNotifier
from test_plans_admin import Env as AdminEnv
from web import security as sec
from web.config import Settings
from web.jobs import InlineExecutor
from web.main import create_app, safe_next
from web.models import Batch, Invitation, Membership, User

ROOT = Path(__file__).parent.parent
TEMPLATES = ROOT / "web" / "templates"
STATIC = ROOT / "web" / "static"
METHODS = ("GET", "POST", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE")


def statuses(client, paths, methods=METHODS):
    out = {}
    for m in methods:
        for p in paths:
            r = client.request(m, p, follow_redirects=False)
            out[(m, p)] = (r.status_code, r.headers.get("location"))
    return out


def raw_asgi(app, method: str, raw_path: str, headers=()):
    """Send a request with an exact path (httpx would normalise '//admin' into a host name)."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": raw_path, "raw_path": raw_path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"testserver"), *headers], "client": ("1.2.3.4", 5), "server": ("testserver", 80)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def run():
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)

    asyncio.run(run())
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


# ---------- hidden areas: one plain 404 for everyone who may not use them (FIX) ----------

ADMIN_PATHS = ("/admin", "/admin/", "/admin/users", "/admin/users/", "/admin/users/SK-AAAAAA",
               "/admin/users/SK-AAAAAA/", "/admin/users/find", "/admin/users/SK-AAAAAA/block", "/admin/requests",
               "/admin/requests/1/done", "/admin/audit", "/admin/audit/", "/Admin", "/ADMIN/users", "/%61dmin",
               "/admin%2Fusers", "/admin/./users", "/static/../admin")


def test_admin_is_one_plain_404_for_strangers_on_every_path_and_method():
    e = AdminEnv()
    anon = e.new_client()
    user = e.login(e.new_client(), "anna@example.com")
    for client in (anon, user):
        for key, (status, location) in statuses(client, ADMIN_PATHS).items():
            assert status == 404 and location is None, key


def test_trailing_slash_never_redirects():
    e = AdminEnv()
    admin = e.admin()
    for client in (e.new_client(), admin):
        for p in ("/admin/", "/admin/users/", "/app/", "/app/history/", "/account/", "/login/", "/join/abc/",
                  "/internal/phone-verified/", "/healthz/", "/contacts/"):
            r = client.get(p, follow_redirects=False)
            assert r.status_code == 404 and "location" not in r.headers, p
    assert admin.get("/admin").status_code == 200  # the real address still works for the owner


def test_raw_double_slash_and_dot_paths_are_unknown():
    e = AdminEnv()
    for path in ("//admin", "//admin/users", "/admin//users", "/./admin", "//internal/phone-verified"):
        assert raw_asgi(e.app, "GET", path) == 404, path


def test_internal_is_a_plain_404_when_phone_confirmation_is_off():
    e = AppEnv()
    c = e.new_client()
    for key, (status, _) in statuses(c, ("/internal/phone-verified", "/internal/phone-verified/", "/internal/x",
                                         "/app/phone", "/app/phone/")).items():
        assert status == 404, key
    e.login()
    for m in ("GET", "POST", "HEAD"):
        assert e.client.request(m, "/app/phone").status_code == 404, m


# ---------- request size: every POST is limited, the answer does not depend on the path (FIX) ----------

def test_every_post_except_upload_has_a_small_body_limit():
    e = AppEnv()
    e.login()
    big = {"content-type": "application/x-www-form-urlencoded"}
    body = b"csrf_token=x&message=" + b"A" * 20_000
    for path in ("/contacts", "/nonexistent", "/admin/users/find", "/login", "/app/company/create", "/app/x/y"):
        r = e.client.post(path, content=body, headers=big)
        assert r.status_code == 413, path


def test_contacts_never_receives_files(monkeypatch):
    import tempfile

    spooled = []
    real = tempfile.SpooledTemporaryFile

    def spy(*a, **kw):
        spooled.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", spy)
    e = AppEnv()
    e.login()
    r = e.client.post("/contacts", data={"csrf_token": csrf(e.client, "/contacts")},
                      files=[("f", ("a.bin", b"x" * 2_000_000, "application/octet-stream"))])
    assert r.status_code == 413 and spooled == []


def test_post_without_length_is_refused():
    e = AppEnv()

    def chunks():  # a generator body is sent chunked, without Content-Length
        yield b"csrf_token=x"

    r = e.new_client().post("/contacts", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 411


# ---------- configuration: development mode cannot run on a public address (FIX) ----------

def test_dev_mode_refuses_a_public_base_url(monkeypatch):
    monkeypatch.setenv("WEB_DEV", "1")
    for url in ("https://skaner.app", "http://203.0.113.5:8000", "http://localhost.evil.com"):
        monkeypatch.setenv("WEB_BASE_URL", url)
        with pytest.raises(RuntimeError):
            Settings.load()
    for url in ("http://localhost:8000", "http://127.0.0.1:8000"):
        monkeypatch.setenv("WEB_BASE_URL", url)
        assert Settings.load().dev


def test_demo_login_cannot_be_switched_on_in_production(monkeypatch):
    monkeypatch.delenv("WEB_DEV", raising=False)
    monkeypatch.setenv("WEB_SECRET", "s" * 48)
    monkeypatch.setenv("WEB_DEMO_EMAIL", "demo@example.com")
    monkeypatch.setenv("WEB_DEMO_CODE", "123456")
    with pytest.raises(RuntimeError):
        Settings.load()
    s = Settings(db_url="sqlite://", secret=SECRET, base_url="https://skaner.app", dev=False,
                 demo_email="demo@example.com", demo_code="123456")
    with pytest.raises(RuntimeError):
        create_app(s, mailer=FakeMailer(), executor=InlineExecutor())
    s = Settings(db_url="sqlite://", secret=SECRET, base_url="https://skaner.app", dev=False, demo_code="123456")
    with pytest.raises(RuntimeError):
        create_app(s, mailer=FakeMailer(), executor=InlineExecutor())


def test_short_secret_is_refused_in_production(monkeypatch):
    monkeypatch.delenv("WEB_DEV", raising=False)
    monkeypatch.setenv("WEB_SECRET", "too-short")
    with pytest.raises(RuntimeError):
        Settings.load()


def test_settings_repr_shows_no_secrets():
    s = Settings(db_url="sqlite://", secret="ROOT-SECRET-" + "q" * 30, base_url="x", dev=True,
                 resend_api_key="re_SECRETKEY", telegram_token="1:SECRETTOKEN",
                 admin_emails=("boss@secret.example",), demo_code="654321")
    text = repr(s) + str(s)
    for secret in ("ROOT-SECRET", "re_SECRETKEY", "SECRETTOKEN", "boss@secret", "654321"):
        assert secret not in text


# ---------- headers, cookies, caching ----------

def test_join_pages_send_no_referrer_and_private_pages_are_not_cached():
    e = CoEnv()
    owner = e.login("owner@example.com")
    assert create(owner).status_code == 303
    token = link_token(owner)
    guest = e.login("guest@example.com")
    r = guest.get(f"/join/{token}")
    assert r.status_code == 200 and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert guest.get("/app").headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert guest.get("/contacts").headers["cache-control"] == "no-store"


def test_security_headers_on_every_kind_of_response():
    e = AppEnv()
    c = e.new_client()
    for path in ("/", "/login", "/privacy", "/static/app.css", "/nonexistent", "/admin"):
        h = c.get(path).headers
        csp = h["content-security-policy"]
        assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp and "frame-ancestors 'none'" in csp, path
        assert "default-src 'self'" in csp and "base-uri 'none'" in csp and "form-action 'self'" in csp
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff", path


def test_production_cookies_are_secure_and_hsts_is_on():
    s = Settings(db_url="sqlite://", secret=SECRET, base_url="https://testserver", dev=False)
    mailer, clock = FakeMailer(), Clock()
    app = create_app(s, mailer=mailer, clock=clock, executor=InlineExecutor(), backend_factory=lambda: None)
    c = TestClient(app, base_url="https://testserver")
    r = c.get("/login")
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")
    tok = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', r.text).group(1)
    r = c.post("/login", data={"email": "anna@example.com", "csrf_token": tok}, follow_redirects=False)
    r = c.post("/login/code", data={"code": mailer.last_code(), "csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303
    cookies = r.headers.get_list("set-cookie")
    session = next(x for x in cookies if x.startswith("sk_session="))
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert flag.lower() in session.lower(), flag
    assert "domain=" not in session.lower()  # host-only: subdomains never receive it
    assert all("secure" in x.lower() and "httponly" in x.lower() for x in cookies)


def test_private_downloads_are_not_cached_and_are_attachments():
    e = AppEnv()
    e.login()
    url = batch_url(upload(e.client, [("a.png", png())]))
    for fmt in ("xlsx", "1c", "csv", "json"):
        r = e.client.get(f"{url}/download?fmt={fmt}")
        assert r.headers["cache-control"] == "no-store" and r.headers["content-disposition"].startswith("attachment")


# ---------- authentication ----------

def test_login_response_does_not_depend_on_the_address():
    e = AppEnv()
    e.login(email="known@example.com")
    answers = []
    for email in ("known@example.com", "new@example.com", "known@example.com"):  # the third one is throttled
        c = e.new_client()
        r = c.post("/login", data={"email": email, "csrf_token": csrf(c, "/login")}, follow_redirects=False)
        answers.append((r.status_code, r.headers["location"]))
    assert len(set(answers)) == 1


def test_code_is_single_use_and_dies_after_five_wrong_tries():
    e = AppEnv()
    c = e.new_client()
    c.post("/login", data={"email": "anna@example.com", "csrf_token": csrf(c, "/login")})
    code = e.mailer.last_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert c.post("/login/code", data={"code": wrong, "csrf_token": csrf(c, "/")},
                      follow_redirects=False).status_code == 400
    r = c.post("/login/code", data={"code": code, "csrf_token": csrf(c, "/")}, follow_redirects=False)
    assert r.status_code == 400  # the right code no longer works: the attempts are used up
    e.clock.advance(seconds=61)
    c2 = e.new_client()
    c2.post("/login", data={"email": "anna@example.com", "csrf_token": csrf(c2, "/login")})
    code = e.mailer.last_code()
    pre_login = c2.cookies.get("sk_login")
    assert c2.post("/login/code", data={"code": code, "csrf_token": csrf(c2, "/")},
                   follow_redirects=False).status_code == 303
    c3 = e.new_client()  # replay of the same code with a copy of the pre-login cookie
    c3.cookies.set("sk_login", pre_login, path="/login")
    assert c3.post("/login/code", data={"code": code, "csrf_token": csrf(c3, "/")},
                   follow_redirects=False).status_code == 400


def test_logout_and_blocking_kill_the_session_on_the_server():
    e = AdminEnv()
    c = e.login(e.new_client(), "anna@example.com")
    stolen = c.cookies.get("sk_session")
    c.post("/logout", data={"csrf_token": csrf(c, "/app")})
    thief = e.new_client()
    thief.cookies.set("sk_session", stolen)
    assert thief.get("/app", follow_redirects=False).status_code == 303  # to /login
    c = e.login(e.new_client(), "anna@example.com")
    stolen = c.cookies.get("sk_session")
    admin = e.admin()
    with e.db() as s:
        code = s.scalar(select(User.account_code).where(User.email_fp == sec.email_fp(e.settings, "anna@example.com")))
    tok = csrf(admin, f"/admin/users/{code}")
    assert admin.post(f"/admin/users/{code}/block", data={"csrf_token": tok}, follow_redirects=False).status_code == 303
    thief = e.new_client()
    thief.cookies.set("sk_session", stolen)
    assert thief.get("/app", follow_redirects=False).status_code == 303


def test_login_replaces_an_existing_session_cookie():
    e = AppEnv()
    c = e.login()
    before = c.cookies.get("sk_session")
    e.login(c)
    assert c.cookies.get("sk_session") != before
    old = e.new_client()
    old.cookies.set("sk_session", before)
    assert old.get("/app", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("value", [
    "//evil.com", "/\\evil.com", "/\t/evil.com", "/\n/evil.com", "https://evil.com", "http:/evil.com",
    "\\\\evil.com", " /x", "/x\r\nSet-Cookie: a=b", "javascript:alert(1)", "/　/evil.com", "", None, 5,
])
def test_safe_next_blocks_open_redirects(value):
    assert safe_next(value) == "/"


def test_login_next_cannot_leave_the_site():
    e = AppEnv()
    c = e.new_client()
    c.post("/login", data={"email": "anna@example.com", "csrf_token": csrf(c, "/login"), "next": "//evil.com/x"})
    r = c.post("/login/code", data={"code": e.mailer.last_code(), "csrf_token": csrf(c, "/")}, follow_redirects=False)
    assert r.headers["location"] == "/account"
    r = c.post("/lang", data={"lang": "en", "next": "https://evil.com", "csrf_token": csrf(c, "/")},
               follow_redirects=False)
    assert r.headers["location"] == "/"


# ---------- CSRF on every state-changing route ----------

CSRF_ROUTES = ("/lang", "/logout", "/contacts", "/app/account/delete", "/app/plans/request", "/app/company/create",
               "/app/company/rename", "/app/company/delete", "/app/company/leave", "/app/company/remove",
               "/app/company/invite-link", "/app/company/invite-code", "/app/company/cancel", "/app/company/answer",
               "/join/" + "a" * 32)


def test_every_form_needs_the_token_and_our_origin():
    e = AppEnv()
    e.login()
    c = e.client
    good = csrf(c, "/app")
    for path in CSRF_ROUTES:
        assert c.post(path, data={"confirm": "delete"}, follow_redirects=False).status_code == 403, path
        assert c.post(path, data={"csrf_token": "0" * 64}, follow_redirects=False).status_code == 403, path
        r = c.post(path, data={"csrf_token": good}, headers={"origin": "https://evil.com"}, follow_redirects=False)
        assert r.status_code == 403, path
        r = c.post(path, data={"csrf_token": good}, headers={"origin": "null"}, follow_redirects=False)
        assert r.status_code == 403, path
        r = c.post(path, data={"csrf_token": good}, headers={"referer": "https://evil.com/x"}, follow_redirects=False)
        assert r.status_code == 403, path
    other = e.new_client()  # a token from another browser does not fit this browser's cookie
    assert c.post("/logout", data={"csrf_token": csrf(other, "/")}, follow_redirects=False).status_code == 403
    assert c.get("/app", follow_redirects=False).status_code == 200  # still signed in


def test_upload_needs_the_token_before_any_file_and_returns_the_memory():
    e = AppEnv()
    e.login()
    c = e.client
    r = upload(c, [("a.png", png())], token="0" * 64)
    assert r.status_code == 403
    r = upload(c, [("a.png", png())], headers={"origin": "https://evil.com"})
    assert r.status_code == 403
    body = (b"--b\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.png\"\r\n\r\n" + png() +
            b"\r\n--b\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n" + csrf(c).encode() +
            b"\r\n--b--\r\n")
    r = c.post("/app/upload", content=body, headers={"content-type": "multipart/form-data; boundary=b"})
    assert r.status_code == 403  # the token came after the file
    upload(c, [("bad.png", b"\x89PNG\r\n\x1a\n" + b"junk" * 100)])  # a broken file
    upload(c, [("a.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>x</script></svg>")])
    upload(c, [("a.heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100)])
    assert e.app.state.memory.used == 0
    with e.db() as s:
        assert s.scalar(select(Batch)) is None
    assert e.calls == []


def test_duplicate_form_fields_take_the_first_value():
    e = AppEnv()
    e.login()
    c = e.client
    tok = csrf(c)
    parts = [("csrf_token", tok), ("fmt", "xlsx"), ("fmt", "pdf_scan"), ("doctype", "receipt"), ("doctype", "zzz")]
    body = b"".join(b"--b\r\nContent-Disposition: form-data; name=\"" + k.encode() + b"\"\r\n\r\n" + v.encode() + b"\r\n"
                    for k, v in parts)
    body += b"--b\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.png\"\r\n\r\n" + png() + b"\r\n--b--\r\n"
    r = c.post("/app/upload", content=body, headers={"content-type": "multipart/form-data; boundary=b"},
               follow_redirects=False)
    assert r.status_code == 303
    with e.db() as s:
        b = s.scalar(select(Batch))
        assert (b.fmt, b.doctype) == ("xlsx", "receipt")


# ---------- authorization: other people's things are a 404 ----------

def test_strangers_and_members_cannot_reach_other_peoples_batches():
    e = CoEnv()
    owner = e.login("owner@example.com")
    url = batch_url(upload(owner, [("a.png", png())]))  # a personal batch of the future owner
    create(owner)
    member = e.login("member@example.com")
    assert join(member, link_token(owner)).status_code == 303
    company_url = batch_url(upload(owner, [("b.png", png())]))
    member_url = batch_url(upload(member, [("c.png", png())]))
    stranger = e.login("stranger@example.com")
    for client, forbidden in ((stranger, (url, company_url, member_url)), (member, (url, company_url))):
        for u in forbidden:
            for suffix in ("", "/status", "/download?fmt=xlsx", "/download?fmt=json"):
                assert client.get(u + suffix).status_code == 404, (u, suffix)
            r = client.post(u + "/delete", data={"csrf_token": csrf(client, "/app")})
            assert r.status_code == 404
    assert owner.get(member_url).status_code == 200  # the owner sees the company's batches (decision 58)
    with e.db() as s:
        assert s.query(Batch).count() == 3


def test_owner_actions_are_404_for_members_and_ids_stay_inside_the_company():
    e = CoEnv()
    a = e.login("a@example.com")
    create(a, "Alpha")
    b = e.login("b@example.com")
    create(b, "Beta")
    m = e.login("m@example.com")
    assert join(m, link_token(a)).status_code == 303
    tok = csrf(m, "/app/company")
    for path, data in (("/app/company/rename", {"name": "Hacked"}), ("/app/company/delete", {"confirm": "delete"}),
                       ("/app/company/invite-link", {}), ("/app/company/invite-code", {"code": "SK-AAAAAA"}),
                       ("/app/company/remove", {"member": "1"}), ("/app/company/cancel", {"invite": "1"})):
        assert m.post(path, data={"csrf_token": tok, **data}).status_code == 404, path
    with e.db() as s:
        member_ids = list(s.scalars(select(Membership.id)).all())
    tok = csrf(b, "/app/company")
    for mid in member_ids:  # Beta's owner cannot remove anyone from Alpha
        assert b.post("/app/company/remove", data={"csrf_token": tok, "member": str(mid)}).status_code == 404
    assert "Alpha" in m.get("/app/company").text


def test_answering_an_invitation_addressed_to_someone_else_is_404():
    e = CoEnv()
    a = e.login("a@example.com")
    create(a)
    victim = e.login("victim@example.com")
    tok = csrf(a, "/app/company")
    a.post("/app/company/invite-code", data={"csrf_token": tok, "code": e.codes["victim@example.com"]})
    with e.db() as s:
        inv = s.scalar(select(Invitation))
    thief = e.login("thief@example.com")
    r = thief.post("/app/company/answer", data={"csrf_token": csrf(thief, "/app/company"), "invite": str(inv.id),
                                                "answer": "accept"})
    assert r.status_code == 404
    assert victim.get("/app/company").status_code == 200


# ---------- output encoding ----------

def test_hostile_names_are_escaped_everywhere():
    e = CoEnv()
    a = e.login("a@example.com")
    evil = '<img src=x onerror=alert(1)>'
    assert create(a, evil).status_code == 303
    page = a.get("/app/company").text
    assert evil not in page and "&lt;img src=x onerror=alert(1)&gt;" in page
    url = batch_url(upload(a, [('<script>alert(1)</script>.png', png())]))
    page = a.get(url).text
    assert "<script>alert(1)</script>" not in page


TURNSTILE_SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js"


def test_templates_have_no_inline_code_or_unescaped_output():
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "|safe" not in text and "autoescape false" not in text, path.name
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path.name  # only external scripts
        assert not re.search(r"\son[a-z]+\s*=", text), path.name  # no inline event handlers
        assert not re.search(r"\sstyle\s*=", text), path.name  # CSP style-src 'self'
        if path.name == "login.html":  # the one allowed outside script: the Turnstile bot check (web/turnstile.py)
            text = text.replace(TURNSTILE_SCRIPT, "")
        assert "http://" not in text and "https://" not in text, path.name  # nothing loaded from elsewhere
    for path in list(STATIC.glob("*.js")) + list(STATIC.glob("*.css")):
        text = path.read_text(encoding="utf-8")
        assert "innerHTML" not in text and "eval(" not in text and "@import" not in text, path.name
        assert not re.search(r"url\(\s*['\"]?https?:", text), path.name


def test_text_pdf_survives_odd_digits_from_a_hostile_document():
    for odd in ("²", "١٢", "5٠", "+3", " 2", "999"):
        cleaned = sanitize_html(f'<table><tr><td colspan="{odd}">x</td></tr></table>')
        assert "colspan" not in cleaned or odd == "x"
    from web.pdf_formats import text_pdf

    data = text_pdf([{"title": "t", "html": '<td colspan="²" rowspan="³">x</td><img src="file:///etc/passwd">'
                                            '<a href="http://evil">l</a><script>s</script>'}], "ru")
    assert data.startswith(b"%PDF") and b"passwd" not in data and b"evil" not in data


def test_only_the_accepted_image_decoders_ever_run(monkeypatch):
    called = []
    real = GifImagePlugin.GifImageFile._open

    def spy(self):
        called.append(1)
        return real(self)

    monkeypatch.setattr(GifImagePlugin.GifImageFile, "_open", spy)
    out = io.BytesIO()
    Image.new("RGB", (4, 4)).save(out, "GIF")
    with pytest.raises(files.UnsupportedFileError):
        files.from_bytes(out.getvalue(), "photo.png")  # the bot path: type from the name, content is a GIF
    xbm = b"#define x_width 8\n#define x_height 8\nstatic char x_bits[] = {" + b"0x00," * 8 + b"};"
    with pytest.raises(files.UnsupportedFileError):
        files.from_bytes(xbm, "photo.jpg")
    assert called == []


# ---------- privacy at rest ----------

def test_database_file_holds_no_email_contact_or_document_text(tmp_path):
    db = tmp_path / "web.sqlite3"
    e = AppEnv(db_url=f"sqlite:///{db}")
    e.plan["a"] = {"docs": [{"merchant_name": "Секретная-Фирма-XYZ", "date": "2026-10-01", "total": 5}]}
    e.login(email="private.person@example.com")
    url = batch_url(upload(e.client, [("a-secret-name.png", png())]))
    assert e.client.get(url).status_code == 200
    c = e.client
    c.post("/app/plans/request", data={"csrf_token": csrf(c, "/app/plans/request"), "plan": "biz",
                                       "company": "ТайнаяКомпания", "contact": "+77015550000", "comment": "x"})
    e.app.state.engine.dispose()
    blob = b""
    for f in tmp_path.iterdir():
        blob += f.read_bytes()
    for needle in ("private.person", "example.com", "Секретная-Фирма-XYZ", "a-secret-name", "ТайнаяКомпания",
                   "77015550000"):
        assert needle.encode("utf-8") not in blob, needle
    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    finally:
        con.close()
    assert "users" in tables
