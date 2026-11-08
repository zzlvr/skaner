"""Plans, "Хочу оплатить" requests, account codes, account page and self-deletion, owner admin (/admin)."""

import json
import logging
import re
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text

from test_app import (  # helpers only; no test functions are imported
    SECRET, Clock, FakeBackend, FakeMailer, all_values, balance_of, batch_url, csrf, ledger, pdf, png, upload,
)
from web import accounts, crypto, payments
from web.config import LANGS, Settings
from web.jobs import InlineExecutor
from web.main import create_app
from web.models import AdminAudit, Batch, LedgerEntry, PaymentRequest, Tombstone, User, UserSession

LOCALES = Path(__file__).parent.parent / "web" / "locales"
ADMIN = "boss@example.com"
TOKEN = "123456:SECRET-bot-token-value"
CONTACT = "+7 701 555 12 34"
COMPANY = "ТОО Секретная-Компания"
COMMENT = "Позвоните после обеда, тайный-комментарий"
CODE_RE = re.compile(r"SK-[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{6}")


class RecordingNotifier:
    configured = True

    def __init__(self, ok=True, boom=False):
        self.sent, self.ok, self.boom = [], ok, boom

    def send(self, text):
        if self.boom:
            raise RuntimeError("network down " + text)
        self.sent.append(text)
        return self.ok


class Env:
    def __init__(self, notifier=None, db_url="sqlite://", admins=(ADMIN,), **settings):
        self.clock, self.mailer = Clock(), FakeMailer()
        self.plan, self.calls = {}, []
        self.notifier = notifier if notifier is not None else RecordingNotifier()
        self.settings = Settings(db_url=db_url, secret=SECRET, base_url="http://testserver", dev=True,
                                 admin_emails=tuple(admins), **settings)
        self.app = create_app(self.settings, mailer=self.mailer, clock=self.clock, executor=InlineExecutor(),
                              backend_factory=lambda: FakeBackend(self.plan, self.calls), notifier=self.notifier)
        self.client = self.new_client()

    def new_client(self, lang="ru"):
        return TestClient(self.app, cookies={"lang": lang})

    def db(self):
        return self.app.state.sessionmaker()

    def login(self, client=None, email="anna@example.com"):
        client = client or self.client
        tok = csrf(client, "/login")
        client.post("/login", data={"email": email, "csrf_token": tok}, follow_redirects=False)
        r = client.post("/login/code", data={"code": self.mailer.last_code(), "csrf_token": csrf(client, "/")},
                        follow_redirects=False)
        self.clock.advance(seconds=61)
        if r.status_code == 303:
            client.get("/app")
        return client

    def admin(self):
        return self.login(self.new_client(), ADMIN)

    def user_by_code(self, code):
        with self.db() as s:
            return s.scalar(select(User).where(User.account_code == code))

    def user(self, index=0):
        return self.users()[index]

    def users(self):
        with self.db() as s:
            return s.scalars(select(User).order_by(User.id)).all()


@pytest.fixture
def env():
    e = Env()
    e.login()
    return e


def anna(env) -> User:
    return env.users()[0]


def send_request(client, plan="biz", company=COMPANY, contact=CONTACT, comment=COMMENT, token=None):
    return client.post("/app/plans/request", data={
        "csrf_token": token or csrf(client, "/app/plans/request"), "plan": plan, "company": company,
        "contact": contact, "comment": comment}, follow_redirects=False)


def admin_post(env, client, path, token=None, **data):
    return client.post(path, data={"csrf_token": token or csrf(client, "/admin"), **data}, follow_redirects=False)


# ---------- plans page ----------

def test_plans_page_for_trial_user(env):
    r = env.client.get("/app/plans")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    t = r.text
    assert "Пробный период: осталось 50 стр., ещё 14 дн." in t and "Тариф не подключён." in t
    for word in ("Старт", "12 900 ₸", "300 страниц", "Сверх: 45 ₸ за страницу", "Бизнес", "34 900 ₸",
                 "Бухгалтерская фирма", "99 900 ₸", "Сверх: 38 ₸ за страницу", "Больше — индивидуально",
                 "согласуем с вами"):
        assert word in t, word
    for k in ("start", "biz", "firm"):
        assert f'href="/app/plans/request?plan={k}">Хочу оплатить' in t
    assert 'href="/app/plans" aria-current="page"' in t  # the sidebar link is real now
    assert "<script>" not in t and " style=" not in t and "onclick" not in t


def test_plans_page_active_and_expired_plan(env):
    a = env.admin()
    code = anna(env).account_code
    assert admin_post(env, a, f"/admin/users/{code}/plan", plan="biz", note="счёт 17").status_code == 303
    t = env.client.get("/app/plans").text
    assert "Тариф «Бизнес»: осталось 1000 из 1000 стр., действует до 19.11.2026." in t
    assert balance_of(env) == 1050
    # The trial ends, the plan runs: no pay link in the sidebar.
    env.clock.advance(days=15)
    assert 'class="spay"' not in env.login(env.new_client()).get("/app").text
    env.clock.advance(days=16)
    c = env.login(env.new_client())
    t = c.get("/app/plans").text
    assert "Тариф «Бизнес» закончился 19.11.2026." in t
    assert 'class="spay"' in c.get("/app").text and "Срок тарифа закончился." in c.get("/app").text
    assert balance_of(env) == 0
    rows = [r for r in ledger(env) if r.reason == "plan_expire"]
    assert len(rows) == 1 and rows[0].plan_delta == -1000 and anna(env).plan_pages_left == 0


def test_pay_link_when_trial_over(env):
    assert 'class="spay"' not in env.client.get("/app").text
    env.clock.advance(days=14, seconds=1)
    c = env.login(env.new_client())
    page = c.get("/app").text
    assert 'class="spay" href="/app/plans">Хочу оплатить' in page and "Пробный период закончился" in page


# ---------- payment request ----------

def test_request_stored_encrypted_and_owner_notified(env):
    form = env.client.get("/app/plans/request?plan=firm")
    assert form.status_code == 200 and '<option value="firm" selected>' in form.text
    r = send_request(env.client)
    assert r.status_code == 200 and "Заявка отправлена" in r.text and "Мы свяжемся с вами" in r.text
    code = anna(env).account_code
    assert code in r.text
    with env.db() as s:
        row = s.scalar(select(PaymentRequest))
        assert (row.plan, row.status, row.user_id) == ("biz", "new", anna(env).id) and row.notified_at is not None
        data = crypto.decrypt_request(env.settings, row.id, row.data_enc)
        assert data["contact"] == CONTACT and data["company"] == COMPANY and data["comment"] == COMMENT
        with pytest.raises(crypto.ResultsUnreadable):
            crypto.decrypt_request(env.settings, row.id + 1, row.data_enc)  # bound to its row
        with pytest.raises(crypto.ResultsUnreadable):
            crypto.decrypt(env.settings, str(row.id), row.data_enc)  # another key
    for needle in (CONTACT, COMPANY, COMMENT, "701 555", "Секретная"):
        for v in all_values(env.app):
            assert needle.encode() not in v if isinstance(v, bytes) else needle not in str(v)
    [msg] = env.notifier.sent
    assert code in msg and CONTACT in msg and COMPANY in msg and "Business" in msg and "admin: /admin/requests" in msg
    assert "@" not in msg


def test_request_validation(env):
    cases = [
        (dict(plan="gold"), "Выберите тариф"),
        (dict(contact=""), "от 3 до 80"), (dict(contact="ab"), "от 3 до 80"), (dict(contact="x" * 81), "от 3 до 80"),
        (dict(contact="+7 701\x00 555"), "служебные символы"), (dict(company="ТОО‮evil"), "служебные символы"),
        (dict(contact="me@mail.example.com"), "Почту указывать не нужно"),
        (dict(comment="write to boss@corp.kz"), "Почту указывать не нужно"),
        (dict(company="x" * 121), "Слишком длинный"), (dict(comment="x" * 501), "Слишком длинный"),
    ]
    for kw, words in cases:
        r = send_request(env.client, **kw)
        assert r.status_code == 400 and words in r.text, kw
    assert send_request(env.client, token="0" * 64).status_code == 403
    assert send_request(env.client, comment="строка 1\nстрока 2", company="", contact="@my_tg").status_code == 200
    with env.db() as s:
        assert s.scalar(select(func.count()).select_from(PaymentRequest)) == 1
    assert env.client.get("/app/plans/request", follow_redirects=False).status_code == 200
    assert env.new_client().get("/app/plans/request", follow_redirects=False).headers["location"].startswith("/login")


def test_hostile_values_escaped_in_form_admin_and_telegram(env):
    evil = '<script>alert(1)</script>'
    r = send_request(env.client, company=evil, contact="")  # refused: the form shows the value back
    assert r.status_code == 400 and "<script>alert" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert send_request(env.client, company=evil, comment='"><img src=x onerror=alert(2)>').status_code == 200
    page = env.admin().get("/admin/requests").text
    assert "<script>alert" not in page and "<img src=x" not in page and "&lt;script&gt;" in page
    msg = env.notifier.sent[-1]
    assert evil in msg  # plain text mode: shown literally, never interpreted


def test_telegram_text_is_plain_and_one_line_per_field():
    form = payments.RequestForm("start", "A\nКомментарий: подделка", "+7 700", "a\n\nadmin: /evil")
    text = payments.notice_text("SK-AAAAAA", form)
    lines = text.split("\n")
    assert len(lines) == 7 and lines[-1] == "admin: /admin/requests" and "/evil" in lines[5]


def test_request_limits_per_user_and_ip(env):
    for _ in range(3):
        assert send_request(env.client).status_code == 200
    r = send_request(env.client)
    assert r.status_code == 429 and "Слишком много заявок" in r.text
    env.clock.advance(days=1, seconds=1)
    c = env.login(env.new_client())
    assert send_request(c).status_code == 200
    # Per IP: 10 a day across accounts (TestClient always comes from the same address).
    e = Env()
    for i in range(4):
        c = e.login(e.new_client(), f"u{i}@example.com")
        for _ in range(3):
            send_request(c)
    with e.db() as s:
        assert s.scalar(select(func.count()).select_from(PaymentRequest)) == 10


def test_telegram_notifier_with_mocked_http(caplog):
    caplog.set_level(logging.DEBUG)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    tg = payments.TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(handler))
    e = Env(notifier=tg)
    e.login()
    assert send_request(e.client).status_code == 200
    [req] = seen
    body = json.loads(req.content)
    assert str(req.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == "42" and "parse_mode" not in body and body["link_preview_options"]["is_disabled"]
    assert CONTACT in body["text"] and "@" not in body["text"]
    assert TOKEN not in repr(tg)
    logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot%s/sendMessage", TOKEN)
    for rec in caplog.records:
        m = rec.getMessage() + repr(rec.args)
        assert TOKEN not in m and CONTACT not in m and COMPANY not in m, m


@pytest.mark.parametrize("notifier", [
    payments.TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    payments.TelegramNotifier(TOKEN, "42", transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow " + TOKEN)))),
    RecordingNotifier(boom=True),
    payments.NullNotifier(),
])
def test_notice_failure_still_saves(notifier, caplog):
    caplog.set_level(logging.DEBUG)
    e = Env(notifier=notifier)
    e.login()
    r = send_request(e.client)
    assert r.status_code == 200 and "Заявка отправлена" in r.text
    with e.db() as s:
        row = s.scalar(select(PaymentRequest))
        assert row is not None and row.notified_at is None
    a = e.admin()
    page = a.get("/admin/requests").text
    assert CONTACT in page and "нет" in page
    for rec in caplog.records:
        m = rec.getMessage() + repr(rec.args)
        assert TOKEN not in m and CONTACT not in m and "network down" not in m, m


def test_request_retention():
    e = Env()
    e.login()
    send_request(e.client)
    send_request(e.client)
    a = e.admin()
    with e.db() as s:
        first, second = s.scalars(select(PaymentRequest).order_by(PaymentRequest.id)).all()
    admin_post(e, a, f"/admin/requests/{first.id}/done")
    e.clock.advance(days=91)
    e.login(e.new_client())  # any cabinet visit purges
    with e.db() as s:
        assert [r.id for r in s.scalars(select(PaymentRequest))] == [second.id]
    e.clock.advance(days=275)
    e.login(e.new_client())
    with e.db() as s:
        assert s.scalar(select(PaymentRequest)) is None


# ---------- account code ----------

def test_account_codes_format_and_uniqueness(env):
    codes = {accounts.new_account_code() for _ in range(2000)}
    assert len(codes) > 1990 and all(CODE_RE.fullmatch(c) for c in codes)
    for i in range(5):
        env.login(env.new_client(), f"p{i}@example.com")
    got = [u.account_code for u in env.users()]
    assert len(set(got)) == 6 and all(CODE_RE.fullmatch(c) for c in got)
    assert accounts.clean_account_code(" sk-" + got[0][3:].lower() + " ") == got[0]
    assert accounts.clean_account_code("SK-0OIL11") is None and accounts.clean_account_code("x" * 40) is None
    page = env.client.get("/app/account").text
    assert got[0] in page and "Код аккаунта" in page


# ---------- money rules ----------

def test_plan_grant_quota_expiry_and_ledger(env):
    a = env.admin()
    u = anna(env)
    r = admin_post(env, a, f"/admin/users/{u.account_code}/plan", plan="start", note="оплата по счёту 5")
    assert r.status_code == 303 and "Plan switched on" in a.get(r.headers["location"]).text
    u = anna(env)
    assert (u.plan, u.plan_quota, u.plan_pages_left) == ("start", 300, 300)
    assert u.plan_expires_at == env.clock() + timedelta(days=30)
    row = ledger(env)[-1]
    assert (row.reason, row.plan_delta, row.note) == ("plan_grant", 300, "оплата по счёту 5")
    # A second activation while running adds the quota and restarts the 30 days.
    env.clock.advance(days=10)
    a = env.admin()  # the earlier admin sign-in is older than 12 hours now
    admin_post(env, a, f"/admin/users/{u.account_code}/plan", plan="biz")
    u = anna(env)
    assert (u.plan, u.plan_pages_left) == ("biz", 1300) and u.plan_expires_at == env.clock() + timedelta(days=30)


def test_charge_order_trial_plan_then_topup(env):
    a = env.admin()
    code = anna(env).account_code
    with env.db() as s:
        s.get(User, anna(env).id).trial_pages_left = 1
        s.commit()
    admin_post(env, a, f"/admin/users/{code}/plan", plan="start")
    with env.db() as s:
        s.get(User, anna(env).id).plan_pages_left = 2
        s.commit()
    admin_post(env, a, f"/admin/users/{code}/topup", pages="10", note="доплата")
    batch_url(upload(env.client, [("long.pdf", pdf(5))]))
    u = anna(env)
    assert (u.trial_pages_left, u.plan_pages_left, u.paid_pages_left) == (0, 0, 8)
    row = ledger(env)[-1]
    assert (row.reason, row.trial_delta, row.plan_delta, row.paid_delta, row.overrun) == ("batch", -1, -2, -2, 0)


def test_expired_plan_pages_not_used_and_never_negative(env):
    a = env.admin()
    code = anna(env).account_code
    admin_post(env, a, f"/admin/users/{code}/plan", plan="start")
    env.clock.advance(days=31)
    c = env.login(env.new_client())
    assert balance_of(env) == 0
    r = upload(c, [("a.png", png())])
    assert r.status_code == 402 and env.calls == []
    # Overrun: 1 page left, a dense page costs 3; balance stops at zero, the rest is recorded.
    admin_post(env, env.admin(), f"/admin/users/{code}/topup", pages="1")
    env.plan["dense"] = {"cost": 0.20}
    batch_url(upload(c, [("dense.png", png())]))
    u = anna(env)
    assert (u.paid_pages_left, u.plan_pages_left) == (0, 0) and balance_of(env) == 0
    row = ledger(env)[-1]
    assert (row.charged, row.paid_delta, row.overrun) == (3, -1, 2)
    # Sums of the ledger deltas are the stored columns.
    rows = [r for r in ledger(env) if r.user_id == u.id]
    assert sum(r.plan_delta for r in rows) == u.plan_pages_left
    assert sum(r.paid_delta for r in rows) == u.paid_pages_left
    assert sum(r.trial_delta for r in rows) == u.trial_pages_left


def test_topup_bounds_and_other_actions(env):
    a = env.admin()
    code = anna(env).account_code
    for bad in ("0", "100001", "-5", "abc", "1.5", "", "9" * 20):
        r = admin_post(env, a, f"/admin/users/{code}/topup", pages=bad)
        assert r.status_code == 400 and "from 1 to 100000" in r.text, bad
    assert admin_post(env, a, f"/admin/users/{code}/topup", pages="5", note="x@y.kz").status_code == 400
    assert admin_post(env, a, f"/admin/users/{code}/topup", pages="100000").status_code == 303
    assert anna(env).paid_pages_left == 100000
    # Extend needs a running plan; end removes the plan's pages.
    assert admin_post(env, a, f"/admin/users/{code}/extend", days="5").status_code == 409
    admin_post(env, a, f"/admin/users/{code}/plan", plan="firm")
    before = anna(env).plan_expires_at
    assert admin_post(env, a, f"/admin/users/{code}/extend", days="5").status_code == 303
    assert anna(env).plan_expires_at == before + timedelta(days=5)
    assert admin_post(env, a, f"/admin/users/{code}/extend", days="367").status_code == 400
    assert admin_post(env, a, f"/admin/users/{code}/end-plan", note="возврат").status_code == 303
    u = anna(env)
    assert u.plan_pages_left == 0 and ledger(env)[-1].plan_delta == -3000 and ledger(env)[-1].note == "возврат"
    # Trial: set and end.
    assert admin_post(env, a, f"/admin/users/{code}/trial", pages="20", days="3").status_code == 303
    assert anna(env).trial_pages_left == 20
    assert admin_post(env, a, f"/admin/users/{code}/trial", pages="1001", days="3").status_code == 400
    assert admin_post(env, a, f"/admin/users/{code}/trial", pages="0", days="0").status_code == 303
    assert balance_of(env) == 100000
    reasons = [r.reason for r in ledger(env)]
    assert {"topup", "plan_grant", "admin_adjust"} <= set(reasons)
    actions = [r.action for r in audit_rows(env)]
    assert actions.count("topup") == 1 and {"plan_grant", "plan_extend", "plan_end", "trial_set"} <= set(actions)


def audit_rows(env):
    with env.db() as s:
        return s.scalars(select(AdminAudit).order_by(AdminAudit.id)).all()


# ---------- admin access ----------

ADMIN_GETS = ("/admin", "/admin/users", "/admin/requests", "/admin/audit", "/admin/users/SK-AAAAAA",
              "/admin/nothing", "/admin/users?code=SK-AAAAAA")
ADMIN_POSTS = ("/admin/users/find", "/admin/users/SK-AAAAAA/topup", "/admin/requests/1/done", "/admin")


def test_admin_is_404_for_anonymous_non_admin_and_when_unset(env):
    anon = env.new_client()
    for c in (anon, env.client):
        tok = csrf(c, "/")
        for path in ADMIN_GETS:
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 404 and r.json() == {"detail": "Not Found"}, path
            assert r.headers["x-robots-tag"] == "noindex, nofollow" and r.headers["cache-control"] == "no-store"
        for path in ADMIN_POSTS:
            for token in (tok, "0" * 64, ""):
                r = c.post(path, data={"csrf_token": token, "pages": "5", "email": "anna@example.com"},
                           follow_redirects=False)
                assert r.status_code == 404, (path, token)
    assert anon.get("/no-such-page").json() == {"detail": "Not Found"}  # same answer as an unknown address
    e = Env(admins=())
    c = e.login(e.new_client(), ADMIN)
    assert c.get("/admin").status_code == 404


def test_admin_pages_headers_and_fresh_login(env):
    a = env.admin()
    for path in ("/admin", "/admin/users", "/admin/requests", "/admin/audit", f"/admin/users/{anna(env).account_code}"):
        r = a.get(path)
        assert r.status_code == 200, path
        assert r.headers["x-robots-tag"] == "noindex, nofollow" and r.headers["cache-control"] == "no-store"
        assert "<script>" not in r.text and " style=" not in r.text and " onclick" not in r.text
    env.clock.advance(hours=12, seconds=1)
    r = a.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/admin&reauth=1"
    assert admin_post(env, a, f"/admin/users/{anna(env).account_code}/topup", token=csrf(a, "/app"),
                      pages="5").status_code == 303
    assert anna(env).paid_pages_left == 0  # refused: redirected to sign in, nothing changed
    page = a.get("/login?next=/admin&reauth=1")
    assert page.status_code == 200 and 'name="next" value="/admin"' in page.text
    assert a.get("/app").status_code == 200  # the cabinet itself still works with the older session
    tok = csrf(a, "/login?next=/admin&reauth=1")
    r = a.post("/login", data={"email": ADMIN, "csrf_token": tok, "next": "/admin"}, follow_redirects=False)
    r = a.post("/login/code", data={"code": env.mailer.last_code(), "csrf_token": csrf(a, "/")}, follow_redirects=False)
    assert r.headers["location"] == "/admin" and a.get("/admin").status_code == 200


def test_every_admin_post_needs_csrf_admin_and_post(env):
    a = env.admin()
    code = anna(env).account_code
    with env.db() as s:
        rid = None
    send_request(env.client)
    with env.db() as s:
        rid = s.scalar(select(PaymentRequest.id))
    posts = [(f"/admin/users/{code}/plan", {"plan": "biz"}), (f"/admin/users/{code}/topup", {"pages": "5"}),
             (f"/admin/users/{code}/extend", {"days": "5"}), (f"/admin/users/{code}/end-plan", {}),
             (f"/admin/users/{code}/trial", {"pages": "5", "days": "5"}), (f"/admin/users/{code}/block", {}),
             (f"/admin/users/{code}/unblock", {}), (f"/admin/users/{code}/delete", {"confirm": code}),
             ("/admin/users/find", {"email": "anna@example.com"}), (f"/admin/requests/{rid}/done", {})]
    foreign = csrf(env.new_client(), "/")
    for path, data in posts:
        for token in ("", "0" * 64, foreign):
            assert a.post(path, data={"csrf_token": token, **data}, follow_redirects=False).status_code == 403, path
        r = a.post(path, data={"csrf_token": csrf(a, "/admin"), **data}, follow_redirects=False,
                   headers={"origin": "https://evil.example"})
        assert r.status_code == 403, path
        tok = csrf(env.client, "/app")
        assert env.client.post(path, data={"csrf_token": tok, **data}, follow_redirects=False).status_code == 404
        assert a.get(path, follow_redirects=False).status_code in (404, 200), path  # never an action by GET
    u = anna(env)
    assert u is not None and u.plan is None and u.paid_pages_left == 0 and u.blocked_at is None
    assert audit_rows(env) == []
    with env.db() as s:
        assert s.get(PaymentRequest, rid).status == "new"


def test_admin_post_rate_limit(env):
    a = env.admin()
    code = anna(env).account_code
    tok = csrf(a, "/admin")
    for _ in range(60):
        assert admin_post(env, a, f"/admin/users/{code}/topup", token=tok, pages="1").status_code == 303
    assert admin_post(env, a, f"/admin/users/{code}/topup", token=tok, pages="1").status_code == 429
    assert anna(env).paid_pages_left == 60


def test_user_search_by_code_and_email(env, caplog):
    caplog.set_level(logging.DEBUG)
    a = env.admin()
    code = anna(env).account_code
    r = a.get("/admin/users?code=" + code.lower(), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/admin/users/{code}"
    assert a.get("/admin/users?code=SK-ZZZZZZ").status_code == 404
    typed = "Anna@Example.com"
    r = admin_post(env, a, "/admin/users/find", email=typed)
    assert r.status_code == 303 and r.headers["location"] == f"/admin/users/{code}"
    r = admin_post(env, a, "/admin/users/find", email="nobody-here@example.com")
    assert r.status_code == 404 and "No account for this email" in r.text and "nobody-here" not in r.text
    detail = a.get(f"/admin/users/{code}").text
    assert code in detail and "Trial" in detail and "trial" in detail  # info + ledger row
    for needle in ("anna@example.com", "nobody-here", "Anna@"):
        for v in all_values(env.app):
            assert needle.encode() not in v if isinstance(v, bytes) else needle not in str(v)
        for rec in caplog.records:
            assert needle not in rec.getMessage() + repr(rec.args)
    assert [r.action for r in audit_rows(env)] == ["find_email", "find_email"]


def test_block_and_unblock(env):
    a = env.admin()
    code = anna(env).account_code
    assert admin_post(env, a, f"/admin/users/{code}/block").status_code == 303
    with env.db() as s:
        assert s.scalar(select(UserSession).where(UserSession.user_id == anna(env).id)) is None
    assert env.client.get("/app", follow_redirects=False).status_code == 303  # signed out everywhere
    c = env.new_client()
    tok = csrf(c, "/login")
    c.post("/login", data={"email": "anna@example.com", "csrf_token": tok}, follow_redirects=False)
    r = c.post("/login/code", data={"code": env.mailer.last_code(), "csrf_token": csrf(c, "/")}, follow_redirects=False)
    assert r.status_code == 403 and "Аккаунт заблокирован" in r.text and "sk_session" not in c.cookies
    env.clock.advance(seconds=61)
    # A session that existed before the block (made directly) cannot use the cabinet or upload.
    from web import auth
    with env.db() as s:
        token = auth.create_session(s, anna(env).id, None, None, env.clock())
    c.cookies.set("sk_session", token)
    assert c.get("/app").status_code == 403 and "Аккаунт заблокирован" in c.get("/app/plans").text
    assert upload(c, [("a.png", png())], token=csrf(c, "/")).status_code == 403 and env.calls == []
    assert admin_post(env, a, f"/admin/users/{code}/unblock").status_code == 303
    assert env.login(env.new_client()).get("/app").status_code == 200
    # The admin cannot block or delete themselves.
    own = env.users()[1].account_code
    assert admin_post(env, a, f"/admin/users/{own}/block").status_code == 409
    assert admin_post(env, a, f"/admin/users/{own}/delete", confirm=own).status_code == 409
    assert [r.action for r in audit_rows(env)] == ["block", "unblock"]


def test_admin_deletes_user(env):
    batch_url(upload(env.client, [("a.png", png())]))
    send_request(env.client)
    a = env.admin()
    u = anna(env)
    assert admin_post(env, a, f"/admin/users/{u.account_code}/delete", confirm="SK-WRONG2").status_code == 400
    r = admin_post(env, a, f"/admin/users/{u.account_code}/delete", confirm=u.account_code.lower())
    assert r.status_code == 303
    check_deleted(env, u)
    [row] = [r for r in audit_rows(env) if r.action == "delete_user"]
    assert row.target_user_id is None and row.target_code == u.account_code
    assert u.account_code in a.get("/admin/audit").text
    assert a.get(f"/admin/users/{u.account_code}").status_code == 404


def check_deleted(env, u):
    with env.db() as s:
        assert s.get(User, u.id) is None
        for model in (Batch, PaymentRequest, UserSession):
            assert s.scalar(select(func.count()).select_from(model).where(model.user_id == u.id)) == 0
        rows = s.scalars(select(LedgerEntry)).all()
        assert not [r for r in rows if r.user_id == u.id]
        assert {"trial_grant", "batch"} <= {r.reason for r in rows if r.user_id is None}
        stone = s.scalar(select(Tombstone).where(Tombstone.email_fp == u.email_fp))
        assert stone is not None and stone.trial_used
    # Registering again: a new account without a second trial.
    c = env.login(env.new_client())
    assert balance_of(env, -1) == 0 and "Пробный период закончился" in c.get("/app").text
    with env.db() as s:
        again = s.scalar(select(User).where(User.email_fp == u.email_fp))
        assert again.account_code != u.account_code  # (SQLite may reuse the id: nothing points to the old one)
        assert s.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.user_id == again.id, LedgerEntry.reason == "trial_grant")) == 0


def test_admin_requests_list_and_mark_done(env):
    send_request(env.client, plan="start")
    a = env.admin()
    page = a.get("/admin/requests").text
    code = anna(env).account_code
    assert CONTACT in page and COMPANY in page and COMMENT in page and f'href="/admin/users/{code}"' in page
    assert "Start" in page and ">Handled<" in page
    assert "New payment requests" in a.get("/admin").text
    with env.db() as s:
        rid = s.scalar(select(PaymentRequest.id))
    assert admin_post(env, a, f"/admin/requests/{rid}/done").status_code == 303
    assert admin_post(env, a, "/admin/requests/999/done").status_code == 404
    assert admin_post(env, a, "/admin/requests/abc/done").status_code == 404
    with env.db() as s:
        row = s.get(PaymentRequest, rid)
        assert row.status == "done" and row.done_at == env.clock()
    assert "handled" in a.get("/admin/requests").text
    detail = [r.detail for r in audit_rows(env) if r.action == "request_done"][0]
    assert CONTACT not in detail and COMPANY not in detail


def test_dashboard_numbers_and_config_status(env):
    batch_url(upload(env.client, [("a.png", png())]))
    env.plan["bad"] = {"fail": True, "cost": 0}
    batch_url(upload(env.client, [("bad.png", png())]))
    send_request(env.client)
    page = env.admin().get("/admin").text
    assert "Users in total / new today</th><td>1 / 1" in page  # the owner is not counted
    assert "Uploads today / failed</th><td>2 / 1" in page
    assert "Recognised pages today / cap</th><td>1 / 300" in page
    assert "New payment requests</th><td><a href=\"/admin/requests\">1</a>" in page
    assert "configured" in page and TOKEN not in page and SECRET not in page


def test_admin_note_escaped(env):
    a = env.admin()
    code = anna(env).account_code
    admin_post(env, a, f"/admin/users/{code}/topup", pages="3", note="<script>alert(1)</script>")
    page = a.get(f"/admin/users/{code}").text
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page


# ---------- account page and self-deletion ----------

def test_account_page(env):
    for path in ("/app/account", "/account"):
        r = env.client.get(path)
        t = r.text
        assert r.status_code == 200 and anna(env).account_code in t and "Вы вошли" in t
        assert "Пробный период: осталось 50 стр." in t and "отпечаток почты" in t and "90 дней" in t
        assert 'action="/logout"' in t and 'action="/app/account/delete"' in t
        assert "пробный период нельзя получить заново" in t
        assert "<script>" not in t and " style=" not in t


def test_self_deletion(env):
    batch_url(upload(env.client, [("a.png", png())]))
    send_request(env.client)
    u = anna(env)
    for word in ("", "да", "удалить аккаунт"):
        r = env.client.post("/app/account/delete", data={"csrf_token": csrf(env.client), "confirm": word},
                            follow_redirects=False)
        assert r.status_code == 400 and "«удалить»" in r.text
    assert env.client.post("/app/account/delete", data={"confirm": "удалить"}).status_code == 403
    assert env.client.get("/app/account/delete").status_code == 405
    assert anna(env) is not None
    r = env.client.post("/app/account/delete", data={"csrf_token": csrf(env.client), "confirm": " Удалить "},
                        follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "sk_session" not in env.client.cookies
    assert env.client.get("/app", follow_redirects=False).status_code == 303
    check_deleted(env, u)


def test_self_deletion_waits_for_running_batch():
    from test_app import ManualExecutor

    e = Env()
    e.app.state.executor = ManualExecutor()
    e.login()
    batch_url(upload(e.client, [("a.png", png())]))
    r = e.client.post("/app/account/delete", data={"csrf_token": csrf(e.client), "confirm": "delete"})
    assert r.status_code == 409 and "после окончания обработки" in r.text


# ---------- languages ----------

def test_locale_parity_and_new_pages_in_three_languages():
    keys = {lang: json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8")) for lang in LANGS}
    assert all(set(keys[lang]) == set(keys["ru"]) for lang in LANGS)
    words = {
        "ru": ("Хочу оплатить", "Заявка на оплату", "Заявка отправлена", "Код аккаунта", "удалить"),
        "kk": ("Төлегім келеді", "Төлемге өтінім", "Өтінім жіберілді", "Аккаунт коды", "жою"),
        "en": ("I want to pay", "Payment request", "Request sent", "Account code", "delete"),
    }
    e = Env()
    for lang in LANGS:
        c = e.login(e.new_client(lang), f"{lang}@example.com")
        pages = (c.get("/app/plans"), c.get("/app/plans/request?plan=start"), send_request(c), c.get("/app/account"),
                 c.get("/app/account"))
        for r, word in zip(pages, words[lang]):
            assert r.status_code == 200 and f'<html lang="{lang}">' in r.text and word in r.text, (lang, word)
        assert "{" not in pages[0].text.split("<main>")[1]  # every placeholder filled
        assert "{" not in pages[3].text.split("<main>")[1]
        r = c.post("/app/account/delete", data={"csrf_token": csrf(c, "/app/account"), "confirm": words[lang][4]},
                   follow_redirects=False)
        assert r.status_code == 303


# ---------- migration ----------

def test_migration_0004_over_existing_data_and_downgrade(tmp_path):
    from alembic import command

    from web.migrate import alembic_config

    url = f"sqlite:///{(tmp_path / 'old.sqlite3').as_posix()}"
    command.upgrade(alembic_config(url), "0003")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        for uid in (1, 2):
            conn.execute(text("INSERT INTO users (id, email_fp, created_at, trial_pages_left, paid_pages_left, "
                              "trial_granted_at, trial_expires_at) VALUES (:id, :fp, '2026-10-20 10:00:00', 40, 3, "
                              "'2026-10-20 10:00:00', '2026-11-03 10:00:00')"), {"id": uid, "fp": str(uid) * 64})
        conn.execute(text("INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at) "
                          "VALUES (:h, 1, '2026-10-20 10:00:00', '2026-11-19 10:00:00', '2026-10-20 10:00:00')"),
                     {"h": "a" * 64})
        conn.execute(text(
            "INSERT INTO batches (public_id, user_id, doctype, fmt, one_document, lang, status, files_count, "
            "files_done, pages_estimated, pages_charged, work_units, docs_found, needs_review, failed_files, "
            "not_found, created_at, expires_at) VALUES ('old_batch_public_id_0001', 1, 'receipt', 'xlsx', 0, 'ru', "
            "'done', 1, 1, 1, 1, 1, 1, 0, 0, 0, '2026-10-20 10:00:00', '2027-01-18 10:00:00')"))
        conn.execute(text("INSERT INTO ledger (user_id, batch_ref, reason, trial_delta, paid_delta, charged, overrun, "
                          "work_units, free_failures, created_at) VALUES (1, 'old_batch_public_id_0001', 'batch', "
                          "-1, 0, 1, 0, 1, 0, '2026-10-20 10:00:00')"))
    eng.dispose()
    command.upgrade(alembic_config(url), "head")
    eng = create_engine(url)
    try:
        insp = inspect(eng)
        assert {"payment_requests", "admin_audit", "tombstones"} <= set(insp.get_table_names())
        with eng.connect() as conn:
            codes = [r[0] for r in conn.execute(text("SELECT account_code FROM users ORDER BY id"))]
            assert len(set(codes)) == 2 and all(CODE_RE.fullmatch(c) for c in codes)
            assert conn.execute(text("SELECT count(*) FROM sessions")).scalar() == 1
            assert conn.execute(text("SELECT count(*) FROM batches")).scalar() == 1
            assert conn.execute(text("SELECT plan_delta, note FROM ledger")).all() == [(0, None)]
            assert conn.execute(text("SELECT plan, plan_quota, plan_pages_left, blocked_at FROM users")).all() == \
                [(None, 0, 0, None)] * 2
    finally:
        eng.dispose()
    # The app works on the migrated database: deleting a user keeps the ledger row unlinked.
    settings = Settings(db_url=url, secret=SECRET, base_url="http://testserver", dev=True)
    app = create_app(settings, mailer=FakeMailer(), clock=Clock(), executor=InlineExecutor(),
                     notifier=payments.NullNotifier())
    with app.state.sessionmaker() as s:
        assert accounts.delete_user(s, 1, Clock()())
        assert s.scalar(select(LedgerEntry)).user_id is None
        assert s.scalar(select(func.count()).select_from(Batch)) == 0
        assert s.scalar(select(User.paid_pages_left).where(User.id == 2)) == 3
    app.state.engine.dispose()
    command.downgrade(alembic_config(url), "0003")
    eng = create_engine(url)
    try:
        insp = inspect(eng)
        assert "payment_requests" not in insp.get_table_names()
        assert "account_code" not in {c["name"] for c in insp.get_columns("users")}
        with eng.connect() as conn:
            assert conn.execute(text("SELECT id, paid_pages_left FROM users")).all() == [(2, 3)]
            assert conn.execute(text("SELECT count(*) FROM ledger")).scalar() == 0  # the unlinked row cannot stay
    finally:
        eng.dispose()
    command.upgrade(alembic_config(url), "head")


# ---------- the summary leaves the owner's own accounts out ----------

def summary(page: str) -> dict:
    """The numbers of /admin as text, keyed by the row's label."""
    rows = re.findall(r'<th scope="row">(.*?)</th><td>(.*?)</td>', page, re.S)
    return {re.sub(r"<[^>]+>", "", k).strip(): re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", v)).strip() for k, v in rows}


def test_summary_ignores_the_owners_own_account_but_counts_customers():
    e = Env()
    owner = e.login(email=ADMIN)
    r = upload(owner, [("a.png", png())])
    assert r.status_code == 303
    page = e.admin().get("/admin").text
    s = summary(page)
    assert s["Users in total / new today"] == "0 / 0"
    assert s["Uploads today / failed"] == "0 / 0"
    assert s["Pages charged today / not covered by the balance"] == "0 / 0"
    assert s["Recognised pages today / cap"].startswith("0 / 300")
    assert "trial pages today: 0 of 100" in s["Recognised pages today / cap"]
    assert "left out" in page
    # the owner's work really did count against the cap and the ledger
    with e.db() as db:
        assert db.scalar(select(func.count()).select_from(Batch)) == 1
    # a customer's batch shows up
    customer = e.login(e.new_client(), "customer@example.com")
    assert upload(customer, [("b.png", png())]).status_code == 303
    s = summary(e.admin().get("/admin").text)
    assert s["Users in total / new today"] == "1 / 1"
    assert s["Uploads today / failed"] == "1 / 0"
    assert s["Recognised pages today / cap"].startswith("1 / 300")
    assert s["Pages charged today / not covered by the balance"].startswith("1 /")


def test_owner_is_marked_in_the_user_list_and_on_the_user_page():
    e = Env()
    e.login(email=ADMIN)
    e.login(e.new_client(), "customer@example.com")
    a = e.admin()
    users_page = a.get("/admin/users").text
    assert users_page.count("(you)") == 1
    mine = next(u for u in e.users() if u.email_fp in e.app.state.admin_fps)
    other = next(u for u in e.users() if u.id != mine.id)
    assert "(you)" in a.get(f"/admin/users/{mine.account_code}").text
    assert "(you)" not in a.get(f"/admin/users/{other.account_code}").text
