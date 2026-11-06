"""Login by one-time email code: flow, limits, privacy, cookies, CSRF, mailer."""

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from web import auth
from web import security as sec
from web.config import KEY_LABELS, LANGS, Settings
from web.db import Base, make_engine
from web.mailer import ConsoleMailer, MailError, ResendMailer, make_mailer
from web.main import create_app
from web.migrate import upgrade_head
from web.models import LoginCode, User, UserSession

SECRET = "test-secret-" + "x" * 40
EMAIL = "Anna.Test@Example.com"
NORM = "anna.test@example.com"
LOCALES = Path(__file__).parent.parent / "web" / "locales"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 20, 12, 0, 0)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, text):
        self.sent.append((to, subject, text))

    def last_code(self):
        return re.search(r"\b(\d{6})\b", self.sent[-1][2]).group(1)


class FailingMailer:
    def send(self, to, subject, text):
        raise MailError("mail API returned 500")


def make(dev=True, db_url="sqlite://", mailer=None, base="http://testserver"):
    clock, mailer = Clock(), mailer or FakeMailer()
    settings = Settings(db_url=db_url, secret=SECRET, base_url=base, dev=dev)
    app = create_app(settings, mailer=mailer, clock=clock)
    return app, clock, mailer


@pytest.fixture
def env():
    app, clock, mailer = make()
    return app, TestClient(app, headers={"accept-language": "ru"}), clock, mailer


def token(client, path="/login"):
    return re.search(r'name="csrf_token" value="([0-9a-f]{64})"', client.get(path).text).group(1)


def ask(client, email=EMAIL, **extra):
    return client.post("/login", data={"email": email, "csrf_token": token(client), **extra}, follow_redirects=False)


def submit(client, code):
    return client.post("/login/code", data={"code": code, "csrf_token": token(client, "/")}, follow_redirects=False)


def wrong(code):
    return f"{(int(code) + 1) % 10**6:06d}"


def login(client, mailer, email=EMAIL):
    assert ask(client, email).status_code == 303
    r = submit(client, mailer.last_code())
    assert r.status_code == 303, r.text
    return r


def db(app):
    return app.state.sessionmaker()


def set_cookie_attrs(response, name):
    for raw in response.headers.get_list("set-cookie"):
        if raw.startswith(name + "="):
            parts = [p.strip() for p in raw.split(";")]
            return {p.split("=")[0].lower(): (p.split("=", 1)[1] if "=" in p else True) for p in parts[1:]}
    raise AssertionError(f"{name} not set")


# ---------- flow ----------

def test_happy_path(env):
    app, client, clock, mailer = env
    assert client.get("/login").status_code == 200
    r = ask(client)
    assert r.status_code == 303 and r.headers["location"] == "/login/code"
    assert len(mailer.sent) == 1 and mailer.sent[0][0] == NORM
    page = client.get("/login/code")
    assert page.status_code == 200 and "Мы отправили код" in page.text
    assert page.headers["cache-control"] == "no-store"

    r = submit(client, mailer.last_code())
    assert r.status_code == 303 and r.headers["location"] == "/account"
    c = set_cookie_attrs(r, "sk_session")
    assert c["httponly"] is True and c["samesite"].lower() == "lax" and c["path"] == "/"
    assert c["max-age"] == str(30 * 24 * 3600) and "secure" not in c  # dev: no Secure
    assert set_cookie_attrs(r, "sk_login")["max-age"] == "0"  # pre-login cookie discarded

    acc = client.get("/account", follow_redirects=False)
    assert acc.status_code == 200 and "Вы вошли" in acc.text
    assert acc.headers["cache-control"] == "no-store"

    out = client.post("/logout", data={"csrf_token": token(client, "/account")}, follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/"
    assert out.headers["cache-control"] == "no-store"
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/account"
    with db(app) as s:
        assert s.scalar(select(UserSession)) is None
        assert s.scalar(select(User)).lang == "ru"


def test_code_page_without_pending_login_goes_back(env):
    _, client, _, _ = env
    assert client.get("/login/code", follow_redirects=False).headers["location"] == "/login"
    r = client.post("/login/code", data={"code": "123456", "csrf_token": token(client)}, follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_pre_login_cookie_expires_after_15_minutes(env):
    _, client, clock, _ = env
    ask(client)
    clock.advance(minutes=16)
    assert client.get("/login/code", follow_redirects=False).headers["location"] == "/login"


def test_wrong_code_counts_attempts_and_locks_after_5(env):
    app, client, _, mailer = env
    ask(client)
    good = mailer.last_code()
    for i in range(5):
        r = submit(client, wrong(good))
        assert r.status_code == 400 and "Код неверный" in r.text
    with db(app) as s:
        assert s.scalar(select(LoginCode.attempts)) == 5
    assert submit(client, good).status_code == 400  # dead after 5 wrong tries


def test_fewer_than_5_wrong_then_right_works(env):
    _, client, _, mailer = env
    ask(client)
    good = mailer.last_code()
    for _ in range(4):
        submit(client, wrong(good))
    assert submit(client, good).status_code == 303


def test_malformed_code_does_not_cost_an_attempt(env):
    app, client, _, mailer = env
    ask(client)
    for bad in ("abc", "12345", "1234567", "x" * 1000, ""):
        assert submit(client, bad).status_code == 400
    with db(app) as s:
        assert s.scalar(select(LoginCode.attempts)) == 0
    code = mailer.last_code()
    assert submit(client, f"{code[:3]} {code[3:]}").status_code == 303  # spaces are fine


def test_expired_code_rejected(env):
    _, client, clock, mailer = env
    ask(client)
    clock.advance(minutes=10, seconds=1)
    assert submit(client, mailer.last_code()).status_code == 400


def test_code_is_single_use(env):
    app, client, _, mailer = env
    ask(client)
    pending = client.cookies.get("sk_login")
    code = mailer.last_code()
    assert submit(client, code).status_code == 303
    client.cookies.set("sk_login", pending, path="/login")
    assert submit(client, code).status_code == 400
    with db(app) as s:
        assert s.scalar(select(UserSession.id).order_by(UserSession.id.desc())) == 1  # only one session made


def test_new_code_invalidates_old(env):
    _, client, clock, mailer = env
    ask(client)
    first = mailer.last_code()
    clock.advance(seconds=61)
    ask(client)
    second = mailer.last_code()
    if first != second:
        assert submit(client, first).status_code == 400
    assert submit(client, second).status_code == 303


def test_resend_cooldown(env):
    _, client, clock, mailer = env
    a, b = ask(client), ask(client)
    assert len(mailer.sent) == 1
    assert (a.status_code, a.headers["location"]) == (b.status_code, b.headers["location"])
    clock.advance(seconds=61)
    ask(client)
    assert len(mailer.sent) == 2


def test_per_email_hourly_and_daily_limits(env):
    _, client, clock, mailer = env
    for _ in range(5):
        assert ask(client).status_code == 303
        clock.advance(seconds=61)
    assert ask(client).status_code == 303  # same response, but nothing sent
    assert len(mailer.sent) == 5
    clock.advance(hours=1)
    for _ in range(5):
        ask(client)
        clock.advance(seconds=61)
    assert len(mailer.sent) == 10
    clock.advance(hours=1)
    ask(client)
    assert len(mailer.sent) == 10  # daily cap
    clock.advance(days=1)
    ask(client)
    assert len(mailer.sent) == 11


def test_per_ip_hourly_limit(env):
    _, client, clock, mailer = env
    for i in range(20):
        assert ask(client, f"user{i}@example.com").status_code == 303
    r = ask(client, "another@example.com")
    assert r.status_code == 429 and "Слишком много попыток" in r.text
    assert len(mailer.sent) == 20
    clock.advance(hours=1, seconds=1)
    assert ask(client, "another@example.com").status_code == 303


def test_per_ip_failed_attempts_limit(env):
    _, client, clock, mailer = env
    ask(client)
    good = mailer.last_code()
    for _ in range(30):
        submit(client, wrong(good))
    assert submit(client, good).status_code == 429


def test_unknown_and_known_email_give_identical_responses(env):
    _, client, clock, mailer = env
    login(TestClient(client.app), mailer, "known@example.com")
    clock.advance(minutes=5)

    def shape(email):
        c = TestClient(client.app)
        r = ask(c, email)
        cookies = sorted(set_cookie_attrs(r, n).items() for n in ("sk_login",))
        return r.status_code, r.headers["location"], r.content, cookies, sorted(k for k in r.headers if k != "set-cookie")

    assert shape("known@example.com") == shape("unknown@example.com")
    # Throttled (cooldown) gives the same response too.
    assert shape("unknown@example.com") == shape("fresh@example.com")


# ---------- privacy ----------

def all_text_values(app):
    out = []
    with app.state.engine.connect() as conn:
        for table in inspect(conn).get_table_names():
            for row in conn.execute(text(f'SELECT * FROM "{table}"')):
                out += [str(v) for v in row if v is not None]
    return out


def test_no_raw_email_code_or_token_in_database(env):
    app, client, _, mailer = env
    ask(client, "  " + EMAIL + " ")
    code = mailer.last_code()
    r = submit(client, code)
    session_token = client.cookies.get("sk_session")
    values = all_text_values(app)
    assert values
    for v in values:
        low = v.lower()
        assert NORM not in low and "anna" not in low and "example" not in low
        assert session_token not in v
        assert v != code and (code not in v or re.fullmatch(r"[0-9a-f]{64}", v))
    with db(app) as s:
        sess = s.scalar(select(UserSession))
        assert sess.token_hash == hashlib.sha256(session_token.encode()).hexdigest()
        assert s.scalar(select(User)).email_fp == sec.email_fp(client.app.state.settings, NORM)


def test_email_code_token_never_logged(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    app, clock, mailer = make()
    client = TestClient(app)
    ask(client)
    code = mailer.last_code()
    submit(client, wrong(code))
    submit(client, code)
    session_token = client.cookies.get("sk_session")
    ask(client, "bad\r\nBcc: x@example.com")
    # A failing mail provider: logged without details.
    app2, _, _ = make(mailer=FailingMailer())
    c2 = TestClient(app2)
    assert ask(c2, "other.person@example.com").status_code == 303
    assert any("login mail not sent" in r.getMessage() for r in caplog.records)
    for rec in caplog.records:
        msg = rec.getMessage().lower() + " " + repr(rec.args).lower()
        for secret in (NORM, "other.person", code, session_token.lower()):
            assert secret not in msg
    printed = capsys.readouterr()
    assert NORM not in printed.out + printed.err


# ---------- cookies and sessions ----------

def test_cookies_secure_outside_dev():
    app, clock, mailer = make(dev=False, base="https://testserver")
    client = TestClient(app, base_url="https://testserver")
    r1 = client.get("/login")
    assert "secure" in set_cookie_attrs(r1, "sk_csrf")
    assert "strict-transport-security" in r1.headers
    r2 = ask(client)
    assert "secure" in set_cookie_attrs(r2, "sk_login") and set_cookie_attrs(r2, "sk_login")["path"] == "/login"
    r3 = submit(client, mailer.last_code())
    c = set_cookie_attrs(r3, "sk_session")
    assert "secure" in c and c["httponly"] is True and c["samesite"].lower() == "lax"
    assert client.get("/account").status_code == 200


def test_sliding_session_and_absolute_cap(env):
    app, client, clock, mailer = env
    login(client, mailer)
    clock.advance(minutes=30)
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 200 and "sk_session" not in r.headers.get("set-cookie", "")  # no write within an hour
    clock.advance(hours=1)
    r = client.get("/account", follow_redirects=False)
    assert set_cookie_attrs(r, "sk_session")["max-age"] == str(30 * 24 * 3600)
    for _ in range(4):  # active every 20 days: stays signed in until the 90-day cap
        clock.advance(days=20)
        assert client.get("/account", follow_redirects=False).status_code == 200
    clock.advance(days=11)
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_idle_session_expires_after_30_days(env):
    _, client, clock, mailer = env
    login(client, mailer)
    clock.advance(days=30, seconds=1)
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303
    assert set_cookie_attrs(r, "sk_session")["max-age"] == "0"  # stale cookie dropped


def test_forged_session_cookie(env):
    _, client, _, _ = env
    client.cookies.set("sk_session", "A" * 43)
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_login_again_replaces_old_session(env):
    app, client, clock, mailer = env
    login(client, mailer)
    old = client.cookies.get("sk_session")
    clock.advance(seconds=61)
    ask(client)  # already signed in: still allowed to sign in again
    submit(client, mailer.last_code())
    assert client.cookies.get("sk_session") != old
    with db(app) as s:
        assert s.scalar(select(UserSession.token_hash)) != hashlib.sha256(old.encode()).hexdigest()
        assert len(s.scalars(select(UserSession)).all()) == 1


def test_revoke_all_sessions(env):
    app, client, clock, mailer = env
    other = TestClient(app)
    login(client, mailer)
    clock.advance(seconds=61)
    login(other, mailer)
    with db(app) as s:
        assert auth.revoke_all_sessions(s, s.scalar(select(User.id))) == 2
    for c in (client, other):
        assert c.get("/account", follow_redirects=False).status_code == 303


def test_signed_in_user_on_login_page_goes_to_account(env):
    _, client, _, mailer = env
    login(client, mailer)
    assert client.get("/login", follow_redirects=False).headers["location"] == "/account"


# ---------- CSRF and redirects ----------

def test_csrf_missing_or_invalid_token(env):
    _, client, _, mailer = env
    client.get("/login")
    for data in ({"email": EMAIL}, {"email": EMAIL, "csrf_token": "0" * 64}, {"email": EMAIL, "csrf_token": "é" * 64}):
        assert client.post("/login", data=data, follow_redirects=False).status_code == 403
    for path in ("/lang", "/logout", "/login/code"):
        assert client.post(path, data={"lang": "kk", "code": "1"}, follow_redirects=False).status_code == 403
    assert mailer.sent == []


def test_csrf_token_bound_to_browser(env):
    app, client, _, _ = env
    foreign = token(TestClient(app))  # valid token of another browser
    client.get("/")
    assert client.post("/login", data={"email": EMAIL, "csrf_token": foreign}).status_code == 403


def test_csrf_cross_origin_rejected(env):
    _, client, _, mailer = env
    t = token(client)
    for headers in ({"origin": "https://evil.example"}, {"origin": "null"}, {"referer": "https://evil.example/x"}):
        r = client.post("/login", data={"email": EMAIL, "csrf_token": t}, headers=headers, follow_redirects=False)
        assert r.status_code == 403
    r = client.post("/login", data={"email": EMAIL, "csrf_token": t}, headers={"origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303 and len(mailer.sent) == 1


def test_logout_needs_csrf_and_keeps_session_otherwise(env):
    _, client, _, mailer = env
    login(client, mailer)
    assert client.post("/logout", data={}, follow_redirects=False).status_code == 403
    assert client.get("/account", follow_redirects=False).status_code == 200


def test_csrf_rotated_on_login(env):
    _, client, _, mailer = env
    client.get("/login")
    before = client.cookies.get("sk_csrf")
    login(client, mailer)
    assert client.cookies.get("sk_csrf") != before


def test_hero_and_language_forms_carry_token(env):
    _, client, _, _ = env
    html = client.get("/").text
    assert html.count('name="csrf_token"') == 2  # language menu + hero form
    assert "<script>" not in html


@pytest.mark.parametrize("nxt,where", [("//evil.com", "/account"), ("https://evil.com", "/account"),
                                        ("/\t/evil.com", "/account"), ("/account?tab=1", "/account?tab=1")])
def test_next_after_login_is_safe(env, nxt, where):
    _, client, _, mailer = env
    ask(client, next=nxt)
    assert submit(client, mailer.last_code()).headers["location"] == where


def test_login_page_next_is_sanitized(env):
    _, client, _, _ = env
    assert "evil" not in client.get("/login?next=//evil.com").text


# ---------- input validation ----------

@pytest.mark.parametrize("bad", [
    "a@b.com\r\nBcc: x@y.com", "a@b.com\nX-Test: 1", "a\x00@b.com", "a@b.com ", "a@b.com\t",
    "a b@c.com", "a@@b.com", "a@b", "@b.com", "a@-b.com", "a..b@c.com", "<a@b.com>", '"a"@b.com', "a@b.c",
    "a@[127.0.0.1]", "a@b.123", "",
])
def test_bad_emails_rejected_without_sending(env, bad):
    _, client, _, mailer = env
    assert ask(client, bad).status_code == 400
    assert mailer.sent == []


def test_oversize_inputs(env):
    _, client, _, mailer = env
    long_email = "a" * 64 + "@" + ".".join(["b" * 60] * 3) + ".com"
    assert len(long_email) > 190 and ask(client, long_email).status_code == 303
    too_long = "a" * 64 + "@" + ".".join(["b" * 60] * 4) + ".com"
    assert len(too_long) > 254 and ask(client, too_long).status_code == 400
    r = client.post("/login", data={"email": "x" * 20000, "csrf_token": token(client)}, follow_redirects=False)
    assert r.status_code == 413


def test_email_normalization_is_stable():
    s = Settings(db_url="sqlite://", secret=SECRET, base_url="http://x", dev=True)
    variants = ["anna@example.com", "  ANNA@Example.COM ", "ａｎｎａ@ｅｘａｍｐｌｅ.ｃｏｍ"]
    assert {sec.email_fp(s, sec.normalize_email(v)) for v in variants} == {sec.email_fp(s, "anna@example.com")}
    assert len(sec.email_fp(s, "anna@example.com")) == 64


# ---------- i18n ----------

def test_locale_key_parity_and_mail_texts():
    keys = {lang: json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8")) for lang in LANGS}
    for lang in LANGS:
        assert set(keys[lang]) == set(keys["ru"])
        assert "{code}" in keys[lang]["mail_body"] and "10" in keys[lang]["mail_body"]


def test_new_pages_render_in_all_languages(env):
    app, _, clock, mailer = env
    words = {"ru": ("Мы отправили код", "Вы вошли"), "kk": ("Поштаңызға код жібердік", "Сіз аккаунтқа кірдіңіз"),
             "en": ("We sent a code", "You are signed in")}
    for lang in LANGS:
        c = TestClient(app, headers={"accept-language": lang})
        ask(c, f"{lang}@example.com")
        page = c.get("/login/code")
        assert f'<html lang="{lang}">' in page.text and words[lang][0] in page.text
        submit(c, mailer.last_code())
        acc = c.get("/account")
        assert f'<html lang="{lang}">' in acc.text and words[lang][1] in acc.text
    subjects = dict(zip(LANGS, [m[1] for m in mailer.sent]))
    assert subjects == {"ru": "Код для входа в Skaner", "kk": "Skaner-ге кіру коды", "en": "Your Skaner sign-in code"}


# ---------- concurrency (real file database, real migrations) ----------

def test_parallel_consume_and_parallel_guesses(tmp_path):
    url = f"sqlite:///{(tmp_path / 'web.sqlite3').as_posix()}"
    app, clock, mailer = make(db_url=url)  # dev + file DB: schema comes from Alembic migrations
    settings = app.state.settings
    try:
        def race(fn, n):
            barrier, results = threading.Barrier(n), []

            def run():
                with app.state.sessionmaker() as s:
                    barrier.wait()
                    results.append(fn(s))

            threads = [threading.Thread(target=run) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert len(results) == n  # every thread finished without an error (e.g. "database is locked")
            return results

        fp = sec.email_fp(settings, NORM)
        with app.state.sessionmaker() as s:
            code = auth.issue_code(s, settings, fp, "ip", clock())
        assert race(lambda s: auth.verify_code(s, settings, fp, code, clock()), 8).count(True) == 1

        clock.advance(seconds=61)
        with app.state.sessionmaker() as s:
            code = auth.issue_code(s, settings, fp, "ip", clock())
        assert not any(race(lambda s: auth.verify_code(s, settings, fp, wrong(code), clock()), 12))
        with app.state.sessionmaker() as s:
            row = s.scalar(select(LoginCode).order_by(LoginCode.id.desc()))
            assert row.attempts == auth.MAX_ATTEMPTS  # parallel guesses cannot exceed the limit
            assert not auth.verify_code(s, settings, fp, code, clock())
    finally:
        app.state.engine.dispose()


def test_interleaved_double_consume(env):
    """Two requests both pass the check before either consumes: only one may win."""
    app, client, clock, mailer = env
    settings = app.state.settings
    fp = sec.email_fp(settings, NORM)
    with db(app) as s1, db(app) as s2:
        code = auth.issue_code(s1, settings, fp, "ip", clock())
        (id1, h1), (id2, h2) = auth.reserve_attempt(s1, fp, clock()), auth.reserve_attempt(s2, fp, clock())
        assert id1 == id2 and h1 == h2 == sec.code_hash(settings, id1, fp, code)
        assert auth.consume(s1, id1, clock()) is True
        assert auth.consume(s2, id2, clock()) is False


def test_attempt_reservation_is_capped(env):
    app, _, clock, _ = env
    settings = app.state.settings
    fp = sec.email_fp(settings, NORM)
    with db(app) as s:
        auth.issue_code(s, settings, fp, "ip", clock())
        got = [auth.reserve_attempt(s, fp, clock())[0] for _ in range(8)]
    assert sum(x is not None for x in got) == auth.MAX_ATTEMPTS


# ---------- schema ----------

def test_migration_matches_models(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    url = f"sqlite:///{(tmp_path / 'm.sqlite3').as_posix()}"
    upgrade_head(url)
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
            assert {"users", "login_codes", "sessions", "rate_events"} <= set(inspect(conn).get_table_names())
    finally:
        engine.dispose()


def test_production_refuses_unmigrated_database(tmp_path):
    url = f"sqlite:///{(tmp_path / 'p.sqlite3').as_posix()}"
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        make(dev=False, db_url=url)
    upgrade_head(url)
    app, _, _ = make(dev=False, db_url=url)
    app.state.engine.dispose()


def test_user_delete_cascades_sessions(env):
    app, client, _, mailer = env
    login(client, mailer)
    with db(app) as s:
        s.delete(s.scalar(select(User)))
        s.commit()
        assert s.scalar(select(UserSession)) is None


# ---------- mailer and settings ----------

def test_console_mailer_prints_only_code_and_masked_address(capsys):
    ConsoleMailer().send("anna.test@example.com", "subj", "Your code: 123456\nignore")
    out = capsys.readouterr().out
    assert "123456" in out and "a***@e***.com" in out
    assert "anna" not in out and "example" not in out and "subj" not in out and "ignore" not in out


def test_resend_mailer_with_mocked_http():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "x"})

    m = ResendMailer("re_key", "Skaner <login@skaner.app>", transport=httpx.MockTransport(handler))
    m.send("anna@example.com", "Code", "123456")
    req = seen[0]
    assert req.method == "POST" and str(req.url) == "https://api.resend.com/emails"
    assert req.headers["authorization"] == "Bearer re_key" and req.headers["idempotency-key"]
    assert json.loads(req.content) == {"from": "Skaner <login@skaner.app>", "to": ["anna@example.com"],
                                       "subject": "Code", "text": "123456"}
    assert "re_key" not in repr(m)


def test_resend_mailer_errors_hide_details():
    def fail(request):
        return httpx.Response(422, json={"message": "bad address anna@example.com"})

    def down(request):
        raise httpx.ConnectError("boom")

    calls = []

    def count(request):
        calls.append(1)
        return httpx.Response(500)

    for handler in (fail, down):
        m = ResendMailer("k", "a@skaner.app", transport=httpx.MockTransport(handler))
        with pytest.raises(MailError) as e:
            m.send("anna@example.com", "s", "123456")
        assert "anna" not in str(e.value) and "123456" not in str(e.value)
    with pytest.raises(MailError):
        ResendMailer("k", "a@skaner.app", transport=httpx.MockTransport(count)).send("x@y.com", "s", "t")
    assert len(calls) == 1  # no retries
    with pytest.raises(MailError):
        ResendMailer("k", "a@skaner.app", transport=httpx.MockTransport(count)).send("x@y.com\r\nBcc: z@y.com", "s", "t")


def test_mailer_selection_and_production_refusal():
    base = dict(db_url="sqlite://", secret=SECRET, base_url="https://skaner.app")
    assert isinstance(make_mailer(Settings(dev=True, **base)), ConsoleMailer)
    with pytest.raises(RuntimeError):
        make_mailer(Settings(dev=False, **base))
    with pytest.raises(RuntimeError):
        create_app(Settings(dev=False, **base))
    with pytest.raises(RuntimeError):
        make_mailer(Settings(dev=False, resend_api_key="k", **base))  # sender missing
    m = make_mailer(Settings(dev=False, resend_api_key="k", mail_from="Skaner <login@skaner.app>", **base))
    assert isinstance(m, ResendMailer)


def test_keys_are_separate_per_purpose():
    s = Settings(db_url="sqlite://", secret=SECRET, base_url="http://x", dev=True)
    keys = {s.key(label) for label in KEY_LABELS}
    assert len(keys) == len(KEY_LABELS) and SECRET.encode() not in keys
    with pytest.raises(ValueError):
        s.key("other")
    assert SECRET not in repr(s)


def test_mail_failure_does_not_leak_to_user():
    app, _, _ = make(mailer=FailingMailer())
    c = TestClient(app)
    r = ask(c)
    assert r.status_code == 303 and r.headers["location"] == "/login/code"
