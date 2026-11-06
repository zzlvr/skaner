"""One trial per phone number, confirmed through the Telegram bot (web/phone.py, bot/phone_verify.py).

Phone numbers here are fictional (+1 202 555 01xx is reserved for fiction in the North American plan)."""

import asyncio
import json
import logging
import re
import threading
from datetime import timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, inspect, select, text

from bot import phone_verify as pv
from bot.main import post_phone_verified
from test_app import SECRET, Clock, FakeMailer, balance_of, batch_url, csrf, ledger, png, upload
from test_plans_admin import Env as BaseEnv
from web import accounts, phone, security as sec
from web.config import LANGS, Settings
from web.jobs import InlineExecutor
from web.main import create_app
from web.models import AdminAudit, LedgerEntry, PhoneChallenge, PhoneTrial, User

PSECRET = "phone-secret-" + "p" * 40
BOT = "SkanerTestBot"
NUM_A, NUM_B = "+1 202 555 0143", "+1 202 555 0177"
DIGITS_A, DIGITS_B = "12025550143", "12025550177"
FP_A, FP_B = pv.phone_fp(PSECRET, DIGITS_A), pv.phone_fp(PSECRET, DIGITS_B)
LINK_RE = re.compile(r'href="https://t\.me/' + BOT + r'\?start=v_([A-Za-z0-9_-]+)"')
ADMIN = "boss@example.com"


def Env(**kw):  # noqa: N802  (a factory that reads like the class it builds)
    kw.setdefault("phone_verify", True)
    kw.setdefault("bot_username", BOT)
    kw.setdefault("phone_verify_secret", PSECRET)
    return BaseEnv(**kw)


@pytest.fixture
def env():
    e = Env()
    e.login()
    return e


def ts_of(env) -> int:
    return int(env.clock().replace(tzinfo=timezone.utc).timestamp())


def get_link(client) -> str:
    r = client.post("/app/phone", data={"csrf_token": csrf(client, "/app/phone")}, follow_redirects=False)
    assert r.status_code == 200, r.text[-2000:]
    return LINK_RE.search(r.text).group(1)


def confirm(env, token, fp=FP_A, ts=None, sig=None, secret=PSECRET, body=None):
    ts = ts_of(env) if ts is None else ts
    payload = body if body is not None else json.dumps({"token": token, "phone_fp": fp, "ts": ts})
    headers = {"Content-Type": "application/json"}
    signature = pv.sign(secret, token, fp, ts) if sig is None else sig
    if signature:
        headers[pv.SIGNATURE_HEADER] = signature
    return env.new_client().post("/internal/phone-verified", content=payload, headers=headers)


def user_of(env, index=0) -> User:
    return env.users()[index]


# ---------- off: old behaviour ----------

def test_feature_off_keeps_the_old_trial_and_hides_everything():
    e = BaseEnv()
    e.login()
    assert balance_of(e) == 50
    page = e.client.get("/app").text
    assert "/app/phone" not in page and "телефон" not in page.lower()
    assert e.client.get("/app/phone").status_code == 404
    r = e.client.post("/app/phone", data={"csrf_token": csrf(e.client, "/app")})
    assert r.status_code == 404
    r = e.new_client().post("/internal/phone-verified", content=b"{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 404
    assert [x.trial_delta for x in ledger(e) if x.reason == "trial_grant"] == [50]


def test_refuses_to_start_without_bot_name_or_secret(monkeypatch):
    for kw in ({"bot_username": ""}, {"phone_verify_secret": "short"}, {"bot_username": "bad name!"},
               {"trial_instant_pages": 60}):
        args = {"phone_verify": True, "bot_username": BOT, "phone_verify_secret": PSECRET, **kw}
        with pytest.raises(RuntimeError):
            create_app(Settings(db_url="sqlite://", secret=SECRET, base_url="http://t", dev=True, **args),
                       mailer=FakeMailer())
    monkeypatch.setenv("WEB_DEV", "1")
    monkeypatch.setenv("WEB_PHONE_VERIFY", "1")
    monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
    monkeypatch.setenv("PHONE_VERIFY_SECRET", PSECRET)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_USERNAME"):
        Settings.load()
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@" + BOT)
    monkeypatch.setenv("PHONE_VERIFY_SECRET", "x" * 31)
    with pytest.raises(RuntimeError, match="PHONE_VERIFY_SECRET"):
        Settings.load()
    monkeypatch.setenv("PHONE_VERIFY_SECRET", PSECRET)
    s = Settings.load()
    assert s.phone_verify and s.bot_username == BOT and s.trial_instant_pages == 0
    assert PSECRET not in repr(s)


# ---------- instant trial, bonus after confirmation ----------

def test_instant_pages_at_first_visit_and_upload_works(env):
    assert balance_of(env) == 10
    assert [(x.reason, x.trial_delta) for x in ledger(env)] == [("trial_grant", 10)]
    page = env.client.get("/app").text
    assert "Подтвердите телефон, чтобы получить ещё 40 пробных стр." in page and 'href="/app/phone"' in page
    batch_url(upload(env.client, [("a.png", png())]))
    assert balance_of(env) == 9


def test_bonus_after_confirmation_keeps_the_end_date(env):
    before = user_of(env)
    token = get_link(env.client)
    env.clock.advance(minutes=3)
    r = confirm(env, token)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    after = user_of(env)
    assert after.trial_pages_left == 50 and after.trial_expires_at == before.trial_expires_at
    assert after.phone_fp == FP_A
    bonus = [x for x in ledger(env) if x.reason == "trial_bonus"]
    assert [(x.trial_delta, x.note) for x in bonus] == [(40, "телефон подтверждён")]
    with env.db() as s:
        row = s.scalar(select(PhoneTrial))
        assert (row.phone_fp, row.first_user_id) == (FP_A, after.id)
    page = env.client.get("/app/phone").text
    assert "Телефон подтверждён" in page
    assert "/app/phone" not in env.client.get("/app").text  # the card is gone
    # The same number again for the same account: harmless, no second bonus.
    with env.db() as s:
        tok = phone.create_challenge(s, s.get(User, after.id), "ip", env.clock())
    assert confirm(env, tok).json() == {"status": "ok"}
    assert user_of(env).trial_pages_left == 50 and len([x for x in ledger(env) if x.reason == "trial_bonus"]) == 1
    # Another number for an account that already has one: refused.
    with env.db() as s:
        tok = phone.create_challenge(s, s.get(User, after.id), "ip", env.clock())
    assert confirm(env, tok, fp=FP_B).json() == {"status": "other_phone"}
    assert user_of(env).phone_fp == FP_A


def test_bonus_after_the_trial_ended_gives_nothing_to_confirm(env):
    env.clock.advance(days=15)
    page = env.client.get("/app").text
    assert "/app/phone" not in page
    assert "не нужно" in env.client.get("/app/phone").text


def test_reused_number_gives_no_bonus_on_another_account(env):
    assert confirm(env, get_link(env.client)).json()["status"] == "ok"
    other = env.login(env.new_client(), "bob@example.com")
    assert balance_of(env, 1) == 10
    r = confirm(env, get_link(other))
    assert r.status_code == 200 and r.json() == {"status": "phone_used"}
    bob = user_of(env, 1)
    assert bob.trial_pages_left == 10 and bob.phone_fp is None
    page = other.get("/app/phone").text
    assert "Этот номер уже получал пробный период. Войдите в тот аккаунт или напишите нам." in page
    # Bob may still confirm a number of his own.
    assert confirm(env, get_link(other), fp=FP_B).json()["status"] == "ok"
    assert user_of(env, 1).trial_pages_left == 50


def test_deleted_account_keeps_the_number_record(env):
    assert confirm(env, get_link(env.client)).json()["status"] == "ok"
    with env.db() as s:
        accounts.delete_user(s, user_of(env).id, env.clock())
        row = s.scalar(select(PhoneTrial))
        assert row.phone_fp == FP_A and row.first_user_id is None
    carol = env.login(env.new_client(), "carol@example.com")
    assert confirm(env, get_link(carol)).json()["status"] == "phone_used"
    assert balance_of(env) == 10


# ---------- the link (challenge) ----------

def test_link_page_token_hashed_and_replaced(env):
    page = env.client.get("/app/phone")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "Подтвердите телефон, чтобы получить ещё 40 пробных страниц" in page.text and "<script>" not in page.text
    first = get_link(env.client)
    assert len("v_" + first) <= 64
    with env.db() as s:
        rows = s.scalars(select(PhoneChallenge)).all()
        assert len(rows) == 1 and rows[0].token_hash == sec.token_hash(first)
        assert rows[0].expires_at == env.clock() + timedelta(minutes=15)
    assert "Ждём подтверждения" in env.client.get("/app/phone").text  # plain reload shows the state
    second = get_link(env.client)
    with env.db() as s:
        assert len(s.scalars(select(PhoneChallenge)).all()) == 1  # one live link per user
    assert confirm(env, first).status_code == 410
    assert confirm(env, second).json()["status"] == "ok"


def test_link_expires_after_15_minutes(env):
    token = get_link(env.client)
    env.clock.advance(minutes=15, seconds=1)
    r = confirm(env, token)
    assert r.status_code == 410 and r.json() == {"status": "invalid"}
    assert "Ссылка устарела" in env.client.get("/app/phone").text
    assert balance_of(env) == 10


def test_link_limits_per_user_and_ip():
    e = Env()
    c = e.login()
    for _ in range(5):
        get_link(c)
    r = c.post("/app/phone", data={"csrf_token": csrf(c, "/app/phone")})
    assert r.status_code == 429 and "Слишком много ссылок" in r.text
    clients = [e.login(e.new_client(), f"u{i}@example.com") for i in range(4)]
    for cl in clients[:3]:
        for _ in range(5):
            get_link(cl)
    r = clients[3].post("/app/phone", data={"csrf_token": csrf(clients[3], "/app/phone")})
    assert r.status_code == 429  # 20 links from this IP today
    e.clock.advance(days=1, seconds=1)
    get_link(clients[3])


def test_link_post_needs_csrf(env):
    assert env.client.post("/app/phone", data={"csrf_token": "0" * 64}).status_code == 403
    with env.db() as s:
        assert s.scalar(select(PhoneChallenge)) is None


# ---------- the internal endpoint ----------

def test_signature_required_wrong_stale_and_replayed(env):
    token = get_link(env.client)
    for r in (confirm(env, token, sig=""), confirm(env, token, sig="0" * 64),
              confirm(env, token, secret="another-secret-" + "q" * 40),
              confirm(env, token, ts=ts_of(env) - 301), confirm(env, token, ts=ts_of(env) + 301),
              confirm(env, token, body=json.dumps({"token": token, "phone_fp": FP_A, "ts": str(ts_of(env))})),
              confirm(env, token, body="not json")):
        assert r.status_code == 403 and r.json() == {"status": "denied"}
    assert balance_of(env) == 10  # nothing happened, the link still works
    ok = confirm(env, token)
    assert ok.json()["status"] == "ok" and ok.headers["cache-control"] == "no-store"
    replay = confirm(env, token)
    assert replay.status_code == 410 and replay.json() == {"status": "invalid"}


def test_unknown_expired_and_used_tokens_answer_the_same(env):
    used = get_link(env.client)
    confirm(env, used)
    other = env.login(env.new_client(), "bob@example.com")
    expired = get_link(other)
    env.clock.advance(minutes=20)
    answers = [confirm(env, tok) for tok in ("u" * 32, used, expired)]
    assert {(r.status_code, r.text) for r in answers} == {(410, '{"status":"invalid"}')}


def test_endpoint_rate_limits(env):
    token = get_link(env.client)
    for _ in range(10):
        assert confirm(env, token, fp="0" * 64, sig="1" * 64).status_code == 403
    for _ in range(phone.CONFIRM_PER_TOKEN_MINUTE):
        confirm(env, "w" * 32)
    assert confirm(env, "w" * 32).status_code == 429  # one token: 10 signed tries a minute
    for _ in range(phone.CONFIRM_PER_MINUTE):
        confirm(env, "z" * 32, sig="1" * 64)
    assert confirm(env, token).status_code == 429  # everyone together: 60 a minute
    env.clock.advance(minutes=1, seconds=1)
    assert confirm(env, token).json()["status"] == "ok"


def test_endpoint_needs_no_session_or_csrf_and_has_no_cookie(env):
    r = confirm(env, get_link(env.client))
    assert r.status_code == 200 and "set-cookie" not in r.headers


def test_single_use_under_parallel_double_submit(tmp_path):
    url = f"sqlite:///{(tmp_path / 'web.sqlite3').as_posix()}"
    e = Env(db_url=url)  # file database, schema from the migrations
    try:
        e.login()
        token = get_link(e.client)
        results, barrier = [], threading.Barrier(8)

        def run():
            with e.app.state.sessionmaker() as s:
                barrier.wait()
                results.append(phone.confirm(s, e.settings, token, FP_A, e.clock()))

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == ["invalid"] * 7 + ["ok"]
        assert user_of(e).trial_pages_left == 50
        assert len([x for x in ledger(e) if x.reason == "trial_bonus"]) == 1
        # Two accounts confirming the same number at the same moment: only one bonus.
        c2, c3 = e.login(e.new_client(), "b@example.com"), e.login(e.new_client(), "c@example.com")
        toks = [get_link(c2), get_link(c3)]
        results.clear()
        barrier = threading.Barrier(2)

        def run2(tok):
            with e.app.state.sessionmaker() as s:
                barrier.wait()
                results.append(phone.confirm(s, e.settings, tok, FP_B, e.clock()))

        threads = [threading.Thread(target=run2, args=(tok,)) for tok in toks]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == ["ok", "phone_used"]
        assert sorted(u.trial_pages_left for u in e.users()[1:]) == [10, 50]
    finally:
        e.app.state.engine.dispose()


def test_bot_client_talks_to_the_site_end_to_end(env):
    """The bot's real request code against the real app (in-process ASGI transport)."""
    token = get_link(env.client)

    async def go(tok, fp):
        transport = httpx.ASGITransport(app=env.app)
        return await post_phone_verified("http://testserver", PSECRET, tok, fp, transport=transport,
                                         now=ts_of(env))

    assert asyncio.run(go(token, FP_A)) == "ok"
    assert asyncio.run(go(token, FP_A)) == "invalid"
    assert user_of(env).trial_pages_left == 50


# ---------- limits and other accounts ----------

def test_per_ip_trial_cap_still_applies():
    e = Env()
    clients = [e.login(e.new_client(), f"user{i}@example.com") for i in range(4)]
    assert [balance_of(e, i) for i in range(4)] == [10, 10, 10, 0]
    page = clients[3].get("/app").text
    assert "с этой сети сегодня недоступен" in page
    assert confirm(e, get_link(clients[3])).json()["status"] == "ok"
    assert balance_of(e, 3) == 0 and user_of(e, 3).phone_fp == FP_A  # the cap still holds today
    e.clock.advance(days=1, seconds=1)
    clients[3].get("/app")
    assert balance_of(e, 3) == 50  # confirmed: the full trial at once
    assert [x.trial_delta for x in ledger(e) if x.reason == "trial_grant"][-1] == 50


def test_upload_points_to_the_phone_step_when_instant_pages_run_out():
    e = Env(trial_instant_pages=1)
    c = e.login()
    r = upload(c, [("a.png", png()), ("b.png", png())])
    assert r.status_code == 402 and "Подтвердите телефон, чтобы получить ещё 49 пробных страниц" in r.text
    assert 'href="/app/phone"' in r.text and e.calls == []
    batch_url(upload(c, [("a.png", png())]))
    r = upload(c, [("a.png", png())])
    assert r.status_code == 402 and "Подтвердите телефон" in r.text and "нужно 1, осталось 0" in r.text
    assert "Подтвердите телефон, чтобы получить ещё 49 пробных стр." in c.get("/app").text  # sidebar
    confirm(e, get_link(c))
    batch_url(upload(c, [("a.png", png()), ("b.png", png())]))
    assert balance_of(e) == 47


def test_demo_account_counts_as_confirmed():
    e = Env(demo_email="demo@skaner.test", demo_code="482915", admins=())
    c = e.new_client()
    c.post("/login", data={"email": "demo@skaner.test", "csrf_token": csrf(c, "/login")})
    c.post("/login/code", data={"code": "482915", "csrf_token": csrf(c, "/")})
    assert "/app/phone" not in c.get("/app").text and balance_of(e) == 50
    assert "не нужно" in c.get("/app/phone").text


def test_accounts_from_before_the_feature_keep_their_trial(tmp_path):
    url = f"sqlite:///{(tmp_path / 'web.sqlite3').as_posix()}"
    old = BaseEnv(db_url=url)
    old.login()
    old.app.state.engine.dispose()
    new = Env(db_url=url)
    try:
        new.clock.advance(minutes=2)  # same mailbox: past the login-code cooldown of the first app
        c = new.login()
        assert balance_of(new) == 50 and "/app/phone" not in c.get("/app").text
        assert c.post("/app/phone", data={"csrf_token": csrf(c, "/app/phone")}).status_code == 200
        with new.db() as s:
            assert s.scalar(select(PhoneChallenge)) is None  # no link for an account that needs none
    finally:
        new.app.state.engine.dispose()


# ---------- admin ----------

def test_admin_shows_state_and_resets_the_binding(env):
    code = user_of(env).account_code
    admin = env.login(env.new_client(), ADMIN)
    page = admin.get(f"/admin/users/{code}").text
    assert "Phone confirmed" in page and "Remove binding" not in page
    confirm(env, get_link(env.client))
    page = admin.get(f"/admin/users/{code}").text
    assert re.search(r"Phone confirmed</th><td>yes", page) and FP_A not in page and "Remove binding" in page
    assert admin.post(f"/admin/users/{code}/phone-reset", data={"csrf_token": "0" * 64}).status_code == 403
    assert env.client.post(f"/admin/users/{code}/phone-reset",
                           data={"csrf_token": csrf(env.client, "/app")}).status_code == 404  # not the owner
    r = admin.post(f"/admin/users/{code}/phone-reset", data={"csrf_token": csrf(admin, "/admin")},
                   follow_redirects=False)
    assert r.status_code == 303
    assert user_of(env).phone_fp is None and user_of(env).trial_pages_left == 50  # pages stay
    with env.db() as s:
        assert s.scalar(select(PhoneTrial)) is None
        row = s.scalar(select(AdminAudit).where(AdminAudit.action == "phone_reset"))
        assert row.target_code == code and FP_A not in row.detail
    assert re.search(r"Phone confirmed</th><td>no", admin.get(f"/admin/users/{code}").text)
    # The freed number can now give a bonus to its new owner.
    dan = env.login(env.new_client(), "dan@example.com")
    assert confirm(env, get_link(dan)).json()["status"] == "ok"
    assert balance_of(env, 2) == 50


# ---------- privacy ----------

def test_no_number_token_or_fingerprint_in_logs_or_tables(caplog):
    caplog.set_level(logging.DEBUG)
    e = Env()
    e.login()
    token = get_link(e.client)
    confirm(e, token, sig="0" * 64)
    confirm(e, token)
    confirm(e, token)
    other = e.login(e.new_client(), "bob@example.com")
    confirm(e, get_link(other))
    messages = [r.getMessage() + " " + repr(r.args) for r in caplog.records]
    assert any("phone confirmation: ok" in m for m in messages)
    secrets_ = (DIGITS_A, FP_A, FP_A[:16], token, pv.sign(PSECRET, token, FP_A, ts_of(e)), PSECRET)
    for m in messages:
        for s in secrets_:
            assert s not in m, m
    # Every table, every cell: the number never; the fingerprint only in the two fingerprint columns.
    allowed = {("users", "phone_fp"), ("phone_trials", "phone_fp")}
    with e.app.state.engine.connect() as conn:
        for table in inspect(conn).get_table_names():
            rows = conn.execute(text(f'SELECT * FROM "{table}"')).mappings().all()
            for row in rows:
                for col, value in row.items():
                    v = value.decode("latin-1") if isinstance(value, bytes) else str(value)
                    assert DIGITS_A not in v and token not in v, (table, col)
                    if (table, col) not in allowed:
                        assert FP_A not in v, (table, col)


# ---------- languages ----------

def test_phone_pages_in_three_languages():
    e = Env()
    words = {
        "ru": ("Подтвердите телефон, чтобы получить ещё 40 пробных стр.", "Получить ссылку на бота", "Открыть Telegram",
               "Телефон подтверждён"),
        "kk": ("Тағы 40 сынақ бетін алу үшін телефонды растаңыз.", "Ботқа сілтеме алу", "Telegram ашу",
               "Телефон расталды"),
        "en": ("Confirm your phone to get 40 more trial pages.", "Get a link to the bot", "Open Telegram",
               "Phone confirmed"),
    }
    fps = {"ru": FP_A, "kk": FP_B, "en": pv.phone_fp(PSECRET, "12025550199")}
    for lang in LANGS:
        c = e.login(e.new_client(lang), f"{lang}@example.com")
        home, start = c.get("/app"), c.get("/app/phone")
        link = c.post("/app/phone", data={"csrf_token": csrf(c, "/app/phone")})
        token = LINK_RE.search(link.text).group(1)
        confirm(e, token, fp=fps[lang])
        done = c.get("/app/phone")
        for r, word in zip((home, start, link, done), words[lang]):
            assert r.status_code == 200 and f'<html lang="{lang}">' in r.text and word in r.text, (lang, word)
            assert "{" not in r.text.split("<main>")[1], lang


# ---------- migration ----------

def test_migration_0005_over_existing_data_and_downgrade(tmp_path):
    from alembic import command

    from web.migrate import alembic_config

    url = f"sqlite:///{(tmp_path / 'old.sqlite3').as_posix()}"
    command.upgrade(alembic_config(url), "0004")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        for uid in (1, 2):
            conn.execute(text("INSERT INTO users (id, email_fp, created_at, trial_pages_left, paid_pages_left, "
                              "trial_granted_at, trial_expires_at, account_code, plan_quota, plan_pages_left) "
                              "VALUES (:id, :fp, '2026-10-20 10:00:00', 40, 3, '2026-10-20 10:00:00', "
                              "'2026-11-03 10:00:00', :code, 0, 0)"),
                         {"id": uid, "fp": str(uid) * 64, "code": f"SK-AAAAA{uid + 1}"})
        conn.execute(text("INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at) "
                          "VALUES (:h, 1, '2026-10-20 10:00:00', '2026-11-19 10:00:00', '2026-10-20 10:00:00')"),
                     {"h": "a" * 64})
        conn.execute(text("INSERT INTO ledger (user_id, reason, trial_delta, paid_delta, plan_delta, charged, overrun, "
                          "work_units, free_failures, created_at) VALUES (1, 'trial_grant', 50, 0, 0, 0, 0, 0, 0, "
                          "'2026-10-20 10:00:00')"))
    eng.dispose()
    command.upgrade(alembic_config(url), "head")
    eng = create_engine(url)
    try:
        insp = inspect(eng)
        assert {"phone_challenges", "phone_trials"} <= set(insp.get_table_names())
        with eng.connect() as conn:
            assert conn.execute(text("SELECT id, phone_fp, trial_pages_left FROM users ORDER BY id")).all() == \
                [(1, None, 40), (2, None, 40)]
            assert conn.execute(text("SELECT count(*) FROM sessions")).scalar() == 1
            assert conn.execute(text("SELECT count(*) FROM ledger")).scalar() == 1
    finally:
        eng.dispose()
    # The app works on the migrated database, with the feature on: old trials stay, two NULL phones fit the index.
    settings = Settings(db_url=url, secret=SECRET, base_url="http://testserver", dev=True, phone_verify=True,
                        bot_username=BOT, phone_verify_secret=PSECRET)
    app = create_app(settings, mailer=FakeMailer(), clock=Clock(), executor=InlineExecutor())
    with app.state.sessionmaker() as s:
        u1 = s.get(User, 1)
        assert not phone.need_phone(s, settings, u1, Clock()(), None)
        tok = phone.create_challenge(s, u1, "ip", Clock()())
        assert phone.confirm(s, settings, tok, FP_A, Clock()()) == "ok"
        assert s.get(User, 1).trial_pages_left == 40  # no bonus for a full trial from before the feature
        assert accounts.delete_user(s, 1, Clock()())
        assert s.scalar(select(PhoneTrial)).first_user_id is None
    app.state.engine.dispose()
    command.downgrade(alembic_config(url), "0004")
    eng = create_engine(url)
    try:
        insp = inspect(eng)
        assert "phone_trials" not in insp.get_table_names() and "phone_challenges" not in insp.get_table_names()
        assert "phone_fp" not in {c["name"] for c in insp.get_columns("users")}
        with eng.connect() as conn:
            assert conn.execute(text("SELECT id, paid_pages_left FROM users")).all() == [(2, 3)]
    finally:
        eng.dispose()
    command.upgrade(alembic_config(url), "head")


def test_phone_protocol_helpers():
    assert pv.normalize_phone("+7 (701) 555-01-43") == "77015550143"
    assert pv.normalize_phone("12025550143") == "12025550143"
    for bad in (None, "", "+0123456789", "123", "+1 202 555 0143 ext 2", "1" * 16, "１２０２５５５０１４３x"):
        assert pv.normalize_phone(bad) is None, bad
    assert pv.token_from_start("v_" + "a" * 32) == "a" * 32
    assert pv.token_from_start("v_bad token") is None and pv.token_from_start("ref_x") is None
    assert pv.fp_key(PSECRET) != PSECRET.encode() and FP_A != FP_B and re.fullmatch(r"[0-9a-f]{64}", FP_A)
    assert not pv.signature_ok(PSECRET, "t" * 32, FP_A, 1, None)
    assert pv.signature_ok(PSECRET, "t" * 32, FP_A, 1, pv.sign(PSECRET, "t" * 32, FP_A, 1))
