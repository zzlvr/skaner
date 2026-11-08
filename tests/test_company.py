"""Company cabinets (web/company.py): creating, invitations by link and by account code, member caps,
the shared page balance, who sees which batch, leaving, removing, deleting, admin view, languages, migration 0006.

Visibility (owner's rule): a member sees only the company batches they made; the owner sees every company batch
plus their own personal ones; nobody else sees anything (404). Personal batches from before joining are not shown
while in a company and come back after leaving; company batches stay with the company."""

import logging
import re
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from test_app import SECRET, Clock, FakeBackend, FakeMailer, ManualExecutor, csrf, ledger, png, upload
from test_plans_admin import ADMIN, RecordingNotifier, admin_post
from web import accounts, balance as bal, company
from web.config import LANGS, Settings
from web.jobs import InlineExecutor
from web.main import create_app
from web.models import Batch, Invitation, LedgerEntry, Membership, Organization, User

NAME = "ТОО Ромашка"
TOKEN_RE = re.compile(r"/join/([A-Za-z0-9_-]{32})")
NEUTRAL = "Если такой аккаунт существует, приглашение отправлено."


class Env:
    def __init__(self, executor=None, db_url="sqlite://", **settings):
        self.clock, self.mailer = Clock(), FakeMailer()
        self.plan, self.calls = {}, []
        self.executor = executor or InlineExecutor()
        self.settings = Settings(db_url=db_url, secret=SECRET, base_url="http://testserver", dev=True,
                                 admin_emails=(ADMIN,), **settings)
        self.app = create_app(self.settings, mailer=self.mailer, clock=self.clock, executor=self.executor,
                              backend_factory=lambda: FakeBackend(self.plan, self.calls), notifier=RecordingNotifier())
        self.codes = {}

    def new_client(self, lang="ru"):
        return TestClient(self.app, cookies={"lang": lang})

    def db(self):
        return self.app.state.sessionmaker()

    def login(self, email, client=None, lang="ru"):
        client = client or self.new_client(lang)
        client.post("/login", data={"email": email, "csrf_token": csrf(client, "/login")}, follow_redirects=False)
        r = client.post("/login/code", data={"code": self.mailer.last_code(), "csrf_token": csrf(client, "/")},
                        follow_redirects=False)
        assert r.status_code == 303
        self.clock.advance(seconds=61)
        client.get("/app")
        with self.db() as s:
            self.codes[email] = s.scalar(select(User).order_by(User.id.desc())).account_code
        return client

    def user(self, email) -> User:
        with self.db() as s:
            return s.scalar(select(User).where(User.account_code == self.codes[email]))

    def pages(self, email) -> int:
        return bal.balance(self.user(email), self.clock()).total


def create(c, name=NAME):
    return c.post("/app/company/create", data={"csrf_token": csrf(c, "/app/company"), "name": name},
                  follow_redirects=False)


def link_token(c) -> str:
    r = c.post("/app/company/invite-link", data={"csrf_token": csrf(c, "/app/company")})
    assert r.status_code == 200, r.text[-1500:]
    return TOKEN_RE.search(r.text).group(1)


def join(c, token, answer="accept"):
    return c.post(f"/join/{token}", data={"csrf_token": csrf(c, f"/join/{token}"), "answer": answer},
                  follow_redirects=False)


def invite_code(c, code):
    return c.post("/app/company/invite-code", data={"csrf_token": csrf(c, "/app/company"), "code": code},
                  follow_redirects=False)


def invite_ids(c) -> list[str]:
    return list(dict.fromkeys(re.findall(r'name="invite" value="(\d+)"', c.get("/app/company").text)))


def answer(c, invite, ans="accept"):
    return c.post("/app/company/answer", data={"csrf_token": csrf(c, "/app/company"), "invite": invite,
                                                "answer": ans}, follow_redirects=False)


def post(c, path, **data):
    return c.post(path, data={"csrf_token": csrf(c, "/app/company"), **data}, follow_redirects=False)


def batch_path(r) -> str:
    assert r.status_code == 303, r.text[:1500]
    return r.headers["location"]


def team(e, members=("a@example.com", "b@example.com")):
    """owner@example.com owns NAME; each email in `members` joined by a link."""
    owner = e.login("owner@example.com")
    assert create(owner).status_code == 303
    clients = {}
    for email in members:
        c = e.login(email)
        assert join(c, link_token(owner)).headers["location"] == "/app/company?done=joined"
        clients[email] = c
    return owner, clients


@pytest.fixture
def env():
    return Env()


# ---------- creating ----------

def test_create_during_trial_and_with_a_plan(env):
    c = env.login("owner@example.com")
    page = c.get("/app/company")
    assert page.status_code == 200 and "Создайте компанию, чтобы дать доступ сотрудникам." in page.text
    assert 'href="/app/company" aria-current="page"' in page.text
    r = create(c)
    assert r.status_code == 303 and r.headers["location"] == "/app/company?done=created"
    page = c.get("/app/company?done=created").text
    assert "Компания создана." in page and NAME in page and "1 из 5" in page and "Владелец" in page
    assert "Страницы компании" in c.get("/app").text  # the sidebar says whose pages are used
    with env.db() as s:
        org = s.scalar(select(Organization))
        assert org.owner_user_id == env.user("owner@example.com").id and len(org.public_id) >= 24
        assert [(m.role, m.user_id) for m in s.scalars(select(Membership))] == [("owner", org.owner_user_id)]
    # A user with an active plan: 10 places.
    p = env.login("paid@example.com")
    admin = env.login(ADMIN)
    assert admin_post(env, admin, f"/admin/users/{env.codes['paid@example.com']}/plan", plan="start").status_code == 303
    create(p, "Paid LLP")
    assert "1 из 10" in p.get("/app/company").text


def test_one_company_per_user_and_member_cannot_create(env):
    owner, clients = team(env, ("a@example.com",))
    assert create(owner, "Second").status_code == 409
    r = create(clients["a@example.com"], "Mine")
    assert r.status_code == 409 and "Вы уже состоите в компании." in r.text
    with env.db() as s:
        assert len(s.scalars(select(Organization)).all()) == 1


@pytest.mark.parametrize("name", ["", "A", "x" * 81, "ТОО\u0007Ромашка", "boss@example.com Ltd", "a‮b"])
def test_bad_company_names_refused(env, name):
    c = env.login("owner@example.com")
    r = create(c, name)
    assert r.status_code == 400 and "Название — от 2 до 80 символов" in r.text
    with env.db() as s:
        assert s.scalar(select(Organization)) is None


def test_hostile_name_is_escaped_everywhere(env):
    evil = '<script>alert(1)</script>"><img src=x>'
    owner = env.login("owner@example.com")
    assert create(owner, evil).status_code == 303
    member = env.login("a@example.com")
    token = link_token(owner)
    invited = env.login("b@example.com")
    invite_code(owner, env.codes["b@example.com"])
    pages = [owner.get("/app/company"), owner.get("/app/plans"), owner.get("/app/account"),
             member.get(f"/join/{token}"), invited.get("/app"), invited.get("/app/company")]
    join(member, token)
    pages += [member.get("/app/company"), member.get("/app/plans")]
    admin = env.login(ADMIN)
    pages.append(admin.get(f"/admin/users/{env.codes['owner@example.com']}"))
    for r in pages:
        assert r.status_code == 200
        assert "<script>alert" not in r.text and '"><img src=x>' not in r.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text, r.url


# ---------- invitation by link ----------

def test_link_invite_accept_then_single_use(env):
    owner = env.login("owner@example.com")
    create(owner)
    token = link_token(owner)
    with env.db() as s:
        inv = s.scalar(select(Invitation))
        assert inv.token_hash != token and len(inv.token_hash) == 64 and inv.kind == "link"
        assert inv.expires_at == env.clock() + timedelta(days=7)
    a = env.login("a@example.com")
    page = a.get(f"/join/{token}")
    assert page.status_code == 200 and f"Компания «{NAME}» приглашает вас." in page.text
    assert join(a, token).headers["location"] == "/app/company?done=joined"
    assert "Вы в компании." in a.get("/app/company?done=joined").text
    b = env.login("b@example.com")
    used = b.get(f"/join/{token}")
    assert used.status_code == 404 and "Приглашение недействительно" in used.text and NAME not in used.text
    assert join(b, token).status_code == 404
    with env.db() as s:
        assert s.scalar(select(Membership).where(Membership.user_id == env.user("b@example.com").id)) is None


def test_link_expired_cancelled_and_declined(env):
    owner = env.login("owner@example.com")
    create(owner)
    old = link_token(owner)
    env.clock.advance(days=7, seconds=1)
    a = env.login("a@example.com")
    assert a.get(f"/join/{old}").status_code == 404
    cancelled = link_token(owner)
    [iid] = re.findall(r'name="invite" value="(\d+)"', owner.get("/app/company").text)
    assert post(owner, "/app/company/cancel", invite=iid).headers["location"] == "/app/company?done=cancelled"
    assert a.get(f"/join/{cancelled}").status_code == 404
    declined = link_token(owner)
    assert join(a, declined, "decline").status_code == 303
    assert a.get(f"/join/{declined}").status_code == 404  # a declined link is closed
    for tok in ("x" * 32, "short", "../../etc"):
        assert a.get(f"/join/{tok}").status_code == 404


def test_signed_out_visitor_signs_in_and_returns(env):
    owner = env.login("owner@example.com")
    create(owner)
    token = link_token(owner)
    c = env.new_client()
    r = c.get(f"/join/{token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/login?next=/join/{token}"
    c.post("/login", data={"email": "new@example.com", "next": f"/join/{token}", "csrf_token": csrf(c, "/login")},
           follow_redirects=False)
    r = c.post("/login/code", data={"code": env.mailer.last_code(), "csrf_token": csrf(c, "/")},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/join/{token}"
    assert f"Компания «{NAME}» приглашает вас." in c.get(f"/join/{token}").text
    assert join(c, token).status_code == 303


def test_link_refused_when_already_in_a_company_or_full():
    e = Env(org_max_trial=3)
    owner, clients = team(e, ("a@example.com", "b@example.com"))
    other = e.login("other@example.com")
    create(other, "Other")
    token = link_token(other)
    r = join(clients["a@example.com"], token)
    assert r.status_code == 409 and "Вы уже состоите в компании" in r.text
    assert "Вы уже состоите в компании" in clients["a@example.com"].get(f"/join/{token}").text
    assert join(clients["a@example.com"], token, "decline").status_code == 409  # the link stays for someone else
    assert join(e.login("c@example.com"), token).status_code == 303  # still usable
    # NAME is full (3 of 3): new links are refused, an old one cannot be used.
    with e.db() as s:
        s.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": e.user("b@example.com").id})
        s.commit()
    spare = link_token(owner)
    join(e.login("d@example.com"), spare)
    r = owner.post("/app/company/invite-link", data={"csrf_token": csrf(owner, "/app/company")})
    assert r.status_code == 409 and "нет свободных мест" in r.text


# ---------- invitation by account code ----------

def test_code_invite_neutral_reply_and_pending_until_accepted(env):
    owner = env.login("owner@example.com")
    create(owner)
    a = env.login("a@example.com")
    right = invite_code(owner, env.codes["a@example.com"].lower())
    wrong = invite_code(owner, "SK-ZZZZZZ" if env.codes["a@example.com"] != "SK-ZZZZZZ" else "SK-YYYYYY")
    assert right.status_code == wrong.status_code == 303 and right.headers["location"] == wrong.headers["location"]
    page = owner.get(right.headers["location"]).text
    assert NEUTRAL in page and page.count("Код SK-") == 2  # both rows look the same
    assert invite_code(owner, "not a code").status_code == 400
    # The invited person sees the invitation; nothing opens before Accept.
    assert f"Компания «{NAME}» приглашает вас." in a.get("/app").text
    with env.db() as s:
        assert s.scalar(select(Membership).where(Membership.user_id == env.user("a@example.com").id)) is None
    url = batch_path(upload(a, [("p.png", png())]))  # still works alone, on the own balance
    assert owner.get(url).status_code == 404
    [iid] = invite_ids(a)
    assert answer(a, iid).headers["location"] == "/app/company?done=joined"
    assert "2 из 5" in owner.get("/app/company").text
    assert answer(a, iid).status_code == 404  # used


def test_code_invite_decline_expiry_and_foreign_ids(env):
    owner = env.login("owner@example.com")
    create(owner)
    a = env.login("a@example.com")
    b = env.login("b@example.com")
    invite_code(owner, env.codes["a@example.com"])
    invite_code(owner, env.codes["b@example.com"])
    [ia] = invite_ids(a)
    [ib] = invite_ids(b)
    assert answer(b, ia).status_code == 404  # an invitation addressed to someone else
    assert answer(a, ia, "decline").headers["location"] == "/app/company?done=declined"
    assert invite_ids(a) == [] and "приглашает" not in a.get("/app").text
    assert owner.get("/app/company").text.count("Код SK-") == 2  # the owner cannot tell a refusal apart
    env.clock.advance(days=7, seconds=1)
    assert "приглашает" not in b.get("/app").text and answer(b, ib).status_code == 404
    with env.db() as s:
        assert {i.status for i in s.scalars(select(Invitation))} == {"declined", "expired"}


def test_code_invite_limits():
    e = Env()
    target = e.login("t@example.com")
    owners = []
    for i in range(6):
        o = e.login(f"o{i}@example.com")
        create(o, f"Co {i}")
        assert invite_code(o, e.codes["t@example.com"]).status_code == 303
        owners.append(o)
    assert len(invite_ids(target)) == 5  # the sixth was silently not delivered
    assert NEUTRAL in owners[5].get("/app/company?done=sent").text and "Код SK-" in owners[5].get("/app/company").text
    # The same code twice: one row.
    invite_code(owners[0], e.codes["t@example.com"])
    with e.db() as s:
        assert len(s.scalars(select(Invitation).where(Invitation.org_id == 1)).all()) == 1
    # 20 live invitations per company (over two days, so the daily limit is not what stops it).
    o = owners[1]
    for _ in range(9):
        link_token(o)
    e.clock.advance(days=1, seconds=1)
    for _ in range(10):
        link_token(o)
    r = invite_code(o, "SK-ABCDEF")
    assert r.status_code == 429 and "Слишком много приглашений" in r.text
    # 20 new per owner per day, even after cancelling.
    for iid in invite_ids(o):
        post(o, "/app/company/cancel", invite=iid)
    for _ in range(10):
        link_token(o)
    assert o.post("/app/company/invite-link", data={"csrf_token": csrf(o, "/app/company")}).status_code == 429
    e.clock.advance(days=1, seconds=1)
    link_token(o)


# ---------- member caps ----------

def test_caps_in_trial_with_plan_and_after_the_plan_ends():
    e = Env()
    owner, _ = team(e, [f"m{i}@example.com" for i in range(4)])  # 5 of 5
    r = owner.post("/app/company/invite-link", data={"csrf_token": csrf(owner, "/app/company")})
    assert r.status_code == 409 and "нет свободных мест" in r.text
    admin = e.login(ADMIN)
    admin_post(e, admin, f"/admin/users/{e.codes['owner@example.com']}/plan", plan="biz")
    for i in range(5):
        assert join(e.login(f"p{i}@example.com"), link_token(owner)).status_code == 303
    assert "10 из 10" in owner.get("/app/company").text
    e.clock.advance(days=31)
    owner = e.login("owner@example.com")
    page = owner.get("/app/company").text
    assert "10 из 5" in page  # nobody is removed
    r = owner.post("/app/company/invite-link", data={"csrf_token": csrf(owner, "/app/company")})
    assert r.status_code == 409


def test_full_company_refuses_an_old_link():
    e = Env(org_max_trial=2)
    owner = e.login("owner@example.com")
    create(owner)
    t1, t2 = link_token(owner), link_token(owner)
    assert join(e.login("a@example.com"), t1).status_code == 303
    r = join(e.login("b@example.com"), t2)
    assert r.status_code == 409 and "нет свободных мест" in r.text
    assert e.login("b@example.com").get(f"/join/{t2}").status_code == 200  # not used up


# ---------- the shared wallet ----------

def test_member_batch_is_paid_by_the_owner(env):
    owner, clients = team(env, ("a@example.com",))
    a = clients["a@example.com"]
    before_owner, before_a = env.pages("owner@example.com"), env.pages("a@example.com")
    batch_path(upload(a, [("1.png", png()), ("2.png", png())]))
    assert env.pages("owner@example.com") == before_owner - 2 and env.pages("a@example.com") == before_a
    row = [x for x in ledger(env) if x.reason == "batch"][-1]
    assert row.user_id == env.user("owner@example.com").id and row.trial_delta == -2
    with env.db() as s:
        b = s.scalar(select(Batch).order_by(Batch.id.desc()))
        assert (b.user_id, b.created_by_user_id, b.wallet_user_id) == (
            env.user("a@example.com").id, env.user("a@example.com").id, env.user("owner@example.com").id)
        assert b.org_id == s.scalar(select(Organization.id))
    side = a.get("/app").text
    assert "Страницы компании" in side and f"<b>{before_owner - 2}</b>" in side
    assert 'class="spay"' not in side  # a member does not send payment requests for the company
    assert "Тариф и страницы компании подключает её владелец." in a.get("/app/plans").text


def test_no_company_pages_left_and_blocked_people(env):
    owner, clients = team(env, ("a@example.com",))
    a = clients["a@example.com"]
    with env.db() as s:
        bal.set_trial(s, s.get(User, env.user("owner@example.com").id), 1, 14, None, env.clock())
    r = upload(a, [("1.png", png()), ("2.png", png())])
    assert r.status_code == 402 and "Не хватает страниц компании: нужно 2, осталось 1." in r.text
    assert env.calls == []
    admin = env.login(ADMIN)
    admin_post(env, admin, f"/admin/users/{env.codes['owner@example.com']}/block")
    r = upload(a, [("1.png", png())])
    assert r.status_code == 403 and "Обработка в этой компании сейчас недоступна" in r.text
    admin_post(env, admin, f"/admin/users/{env.codes['owner@example.com']}/unblock")
    batch_path(upload(a, [("1.png", png())]))
    admin_post(env, admin, f"/admin/users/{env.codes['a@example.com']}/block")
    assert a.get("/app", follow_redirects=False).status_code in (303, 403)  # blocked: no session or blocked page


def test_two_members_cannot_overspend_the_wallet():
    ex = ManualExecutor()
    e = Env(executor=ex)
    owner, clients = team(e, ("a@example.com", "b@example.com"))
    with e.db() as s:
        bal.set_trial(s, s.get(User, e.user("owner@example.com").id), 3, 14, None, e.clock())
    batch_path(upload(clients["a@example.com"], [("1.png", png()), ("2.png", png())]))  # queued: 2 reserved
    r = upload(clients["b@example.com"], [("1.png", png()), ("2.png", png())])
    assert r.status_code == 402 and "нужно 2, осталось 1" in r.text
    batch_path(upload(owner, [("1.png", png())]))
    r = upload(clients["b@example.com"], [("1.png", png())])
    assert r.status_code == 402
    ex.run()
    assert e.pages("owner@example.com") == 0
    assert sum(x.overrun for x in ledger(e)) == 0


def test_parallel_uploads_from_two_members(tmp_path):
    url = f"sqlite:///{(tmp_path / 'web.sqlite3').as_posix()}"
    ex = ManualExecutor()
    e = Env(executor=ex, db_url=url)
    try:
        owner, clients = team(e, ("a@example.com", "b@example.com"))
        with e.db() as s:
            bal.set_trial(s, s.get(User, e.user("owner@example.com").id), 3, 14, None, e.clock())
        tokens = {k: csrf(c) for k, c in clients.items()}
        barrier, results = threading.Barrier(2), {}

        def go(key):
            barrier.wait()
            results[key] = upload(clients[key], [("1.png", png()), ("2.png", png())], token=tokens[key]).status_code

        threads = [threading.Thread(target=go, args=(k,)) for k in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results.values()) in ([303, 402], [402, 402])  # never both
        with e.db() as s:
            assert sum(b.pages_estimated for b in s.scalars(select(Batch))) <= 3
        ex.run()
        assert e.pages("owner@example.com") >= 0 and sum(x.overrun for x in ledger(e)) == 0
    finally:
        e.app.state.engine.dispose()


def test_no_company_works_as_before(env):
    c = env.login("solo@example.com")
    batch_path(upload(c, [("1.png", png())]))
    assert env.pages("solo@example.com") == 49
    with env.db() as s:
        b = s.scalar(select(Batch))
        assert b.org_id is None and b.wallet_user_id == b.user_id == b.created_by_user_id
    hist = c.get("/app/history").text
    assert "Кто загрузил" not in hist and "Страницы компании" not in c.get("/app").text


# ---------- who sees which batch ----------

ROUTES = ("", "/status", "/download?fmt=xlsx")


def reachable(c, url) -> list[int]:
    return [c.get(url + suffix).status_code for suffix in ROUTES]


def test_isolation_of_company_batches(env):
    owner, clients = team(env, ("a@example.com", "b@example.com"))
    a, b = clients["a@example.com"], clients["b@example.com"]
    stranger = env.login("s@example.com")
    other = env.login("x-owner@example.com")
    create(other, "Other")
    x = env.login("x@example.com")
    join(x, link_token(other))
    url = batch_path(upload(a, [("1.png", png())]))
    assert reachable(a, url) == [200, 200, 200]
    owner_view = owner.get(url).text
    assert reachable(owner, url) == [200, 200, 200] and f"Загрузил: {env.codes['a@example.com']}" in owner_view
    for c in (b, stranger, other, x):
        assert reachable(c, url) == [404, 404, 404]
        assert post(c, url + "/delete").status_code == 404
    hist = owner.get("/app/history").text
    assert url in hist and "Кто загрузил" in hist and env.codes["a@example.com"] in hist
    assert url not in b.get("/app/history").text and url in a.get("/app/history").text
    assert "Кто загрузил" not in a.get("/app/history").text
    # The owner's own batch: not visible to a member.
    own = batch_path(upload(owner, [("1.png", png())]))
    assert reachable(a, own) == [404, 404, 404] and own not in a.get("/app/history").text
    # Removed: no access at once; the owner keeps it.
    with env.db() as s:
        mid = s.scalar(select(Membership.id).where(Membership.user_id == env.user("a@example.com").id))
    assert post(owner, "/app/company/remove", member=str(mid)).headers["location"] == "/app/company?done=removed"
    assert reachable(a, url) == [404, 404, 404] and url not in a.get("/app/history").text
    assert reachable(owner, url) == [200, 200, 200]
    assert post(a, url + "/delete").status_code == 404


def test_personal_batches_before_joining_and_after_leaving(env):
    owner = env.login("owner@example.com")
    before_owner = batch_path(upload(owner, [("1.png", png())]))
    create(owner)
    a = env.login("a@example.com")
    personal = batch_path(upload(a, [("1.png", png())]))
    join(a, link_token(owner))
    assert reachable(a, personal) == [404, 404, 404] and personal not in a.get("/app/history").text
    assert reachable(owner, personal) == [404, 404, 404]
    assert reachable(owner, before_owner) == [200, 200, 200] and reachable(a, before_owner) == [404, 404, 404]
    made = batch_path(upload(a, [("2.png", png())]))
    r = a.post("/app/company/leave", data={"csrf_token": csrf(a, "/app/company")}, follow_redirects=False)
    assert r.headers["location"] == "/app/company?done=left"
    assert reachable(a, personal) == [200, 200, 200] and reachable(a, made) == [404, 404, 404]
    assert reachable(owner, made) == [200, 200, 200]
    assert "Создайте компанию" in a.get("/app/company").text


def test_delete_rights(env):
    owner, clients = team(env, ("a@example.com", "b@example.com"))
    a = clients["a@example.com"]
    first = batch_path(upload(a, [("1.png", png())]))
    second = batch_path(upload(a, [("2.png", png())]))
    assert 'action="' + first + '/delete"' in a.get(first).text
    assert post(clients["b@example.com"], first + "/delete").status_code == 404
    assert post(a, first + "/delete").headers["location"] == "/app/history"
    assert post(owner, second + "/delete").headers["location"] == "/app/history"
    with env.db() as s:
        assert s.scalar(select(Batch)) is None


def test_member_of_another_company_and_fellow_member_cannot_list_or_guess(env):
    owner, clients = team(env, ("a@example.com", "b@example.com"))
    url = batch_path(upload(clients["a@example.com"], [("1.png", png())]))
    pid = url.rsplit("/", 1)[1]
    for c in (clients["b@example.com"], env.login("s@example.com")):
        assert pid not in c.get("/app/history").text
        assert c.get(url + "/download?fmt=json").status_code == 404


# ---------- leaving, removing, deleting ----------

def test_owner_cannot_leave_and_owner_only_actions_are_404_for_members(env):
    owner, clients = team(env, ("a@example.com",))
    r = owner.post("/app/company/leave", data={"csrf_token": csrf(owner, "/app/company")})
    assert r.status_code == 409 and "Владелец не может выйти" in r.text
    a = clients["a@example.com"]
    for path, data in (("/app/company/invite-link", {}), ("/app/company/invite-code", {"code": "SK-ABCDEF"}),
                       ("/app/company/rename", {"name": "Hack"}), ("/app/company/delete", {"confirm": "удалить"}),
                       ("/app/company/remove", {"member": "1"}), ("/app/company/cancel", {"invite": "1"})):
        assert post(a, path, **data).status_code == 404, path
    stranger = env.login("s@example.com")
    assert post(stranger, "/app/company/rename", name="Hack").status_code == 404
    with env.db() as s:
        own = s.scalar(select(Membership.id).where(Membership.role == "owner"))
    assert post(owner, "/app/company/remove", member=str(own)).status_code == 404  # not the owner's own row
    assert post(owner, "/app/company/rename", name="Новое имя").headers["location"] == "/app/company?done=renamed"
    assert "Новое имя" in a.get("/app/company").text


def test_every_company_post_needs_csrf(env):
    owner, clients = team(env, ("a@example.com",))
    token = link_token(owner)
    bad = {"csrf_token": "0" * 64}
    for c, path in ((owner, "/app/company/create"), (owner, "/app/company/invite-link"),
                    (owner, "/app/company/rename"), (owner, "/app/company/delete"),
                    (clients["a@example.com"], "/app/company/leave"), (owner, "/app/company/remove"),
                    (owner, "/app/company/cancel"), (owner, "/app/company/answer"), (owner, f"/join/{token}")):
        assert c.post(path, data=bad).status_code == 403, path
    assert env.new_client().post("/app/company/create", data=bad, follow_redirects=False).status_code == 403


def test_delete_company_keeps_batches_with_their_authors():
    ex = ManualExecutor()
    e = Env(executor=ex)
    owner, clients = team(e, ("a@example.com",))
    a = clients["a@example.com"]
    url = batch_path(upload(a, [("1.png", png())]))  # queued: the company cannot be deleted now
    r = post(owner, "/app/company/delete", confirm="удалить")
    assert r.status_code == 409 and "после окончания обработки" in r.text
    ex.run()
    assert post(owner, "/app/company/delete", confirm="нет").status_code == 400
    assert post(owner, "/app/company/delete", confirm="delete").headers["location"] == "/app/company?done=deleted"
    with e.db() as s:
        assert s.scalar(select(Organization)) is None and s.scalar(select(Membership)) is None
        assert s.scalar(select(Batch)).org_id is None
    assert reachable(a, url) == [200, 200, 200] and reachable(owner, url) == [404, 404, 404]


def test_account_deletion_rules(env):
    owner, clients = team(env, ("a@example.com",))
    a = clients["a@example.com"]
    r = owner.post("/app/account/delete", data={"csrf_token": csrf(owner, "/app/account"), "confirm": "удалить"})
    assert r.status_code == 409 and "Сначала уберите их или удалите компанию" in r.text
    admin = env.login(ADMIN)
    r = admin_post(env, admin, f"/admin/users/{env.codes['owner@example.com']}/delete",
                   confirm=env.codes["owner@example.com"])
    assert r.status_code == 409
    with env.db() as s:
        with pytest.raises(RuntimeError):
            accounts.delete_user(s, env.user("owner@example.com").id, env.clock())
        s.rollback()
    batch_path(upload(a, [("1.png", png())]))
    invited = env.login("i@example.com")
    invite_code(owner, env.codes["i@example.com"])
    # The member deletes the account: membership, invitations and the member's batches go.
    assert a.post("/app/account/delete", data={"csrf_token": csrf(a, "/app/account"), "confirm": "удалить"},
                  follow_redirects=False).status_code == 303
    assert "1 из 5" in owner.get("/app/company").text
    with env.db() as s:
        assert s.scalar(select(Batch)) is None
    # The owner alone: the company goes with the account; the invitation addressed to someone else too.
    assert owner.post("/app/account/delete", data={"csrf_token": csrf(owner, "/app/account"), "confirm": "удалить"},
                      follow_redirects=False).status_code == 303
    with env.db() as s:
        assert s.scalar(select(Organization)) is None and s.scalar(select(Invitation)) is None
        assert s.scalar(select(Membership)) is None
    assert invite_ids(invited) == []
    # Ledger rows stay (unlinked), as before.
    assert all(x.user_id is None for x in ledger(env) if x.reason == "batch")


def test_invited_user_deleting_the_account_removes_the_invitation(env):
    owner = env.login("owner@example.com")
    create(owner)
    i = env.login("i@example.com")
    invite_code(owner, env.codes["i@example.com"])
    with env.db() as s:
        accounts.delete_user(s, env.user("i@example.com").id, env.clock())
        assert s.scalar(select(Invitation)) is None


# ---------- admin ----------

def test_admin_shows_the_company_and_removes_a_member(env):
    owner, clients = team(env, ("a@example.com",))
    admin = env.login(ADMIN)
    page = admin.get(f"/admin/users/{env.codes['owner@example.com']}").text
    assert NAME in page and "owner" in page and "People in the company</th><td>2" in page
    assert env.codes["a@example.com"] in page and "/leave-org" not in page
    page = admin.get(f"/admin/users/{env.codes['a@example.com']}").text
    assert "member" in page and "/leave-org" in page
    assert clients["a@example.com"].post(f"/admin/users/{env.codes['a@example.com']}/leave-org",
                                         data={"csrf_token": csrf(clients["a@example.com"])}).status_code == 404
    r = admin_post(env, admin, f"/admin/users/{env.codes['a@example.com']}/leave-org")
    assert r.status_code == 303
    assert "User removed from the company." in admin.get(r.headers["location"]).text
    assert admin_post(env, admin, f"/admin/users/{env.codes['owner@example.com']}/leave-org").status_code == 409
    assert "Создайте компанию" in clients["a@example.com"].get("/app/company").text
    assert "Company</th><td>none" in admin.get(f"/admin/users/{env.codes['a@example.com']}").text


# ---------- privacy, logs, languages ----------

def test_no_emails_codes_or_names_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    e = Env()
    owner, clients = team(e, ("a@example.com",))
    invite_code(owner, e.codes["a@example.com"])
    invite_code(owner, "SK-ABCDEF")
    messages = [r.getMessage() + " " + repr(r.args) for r in caplog.records]
    assert any("member joined" in m for m in messages)
    for m in messages:
        for secret in (NAME, "owner@example.com", "a@example.com", e.codes["a@example.com"], "SK-ABCDEF"):
            assert secret not in m, m


def test_company_pages_in_three_languages():
    e = Env()
    words = {
        "ru": ("Создайте компанию, чтобы дать доступ сотрудникам.", "Пригласить по ссылке", "Выйти из компании",
               "Приглашение в компанию"),
        "kk": ("Қызметкерлерге қол жеткізу беру үшін компания құрыңыз.", "Сілтеме арқылы шақыру",
               "Компаниядан шығу", "Компанияға шақыру"),
        "en": ("Create a company to give your staff access.", "Invite by link", "Leave the company",
               "Invitation to a company"),
    }
    for lang in LANGS:
        owner = e.login(f"o-{lang}@example.com", lang=lang)
        none = owner.get("/app/company")
        create(owner, f"Co {lang}")
        own = owner.get("/app/company")
        token = link_token(owner)
        member = e.login(f"m-{lang}@example.com", lang=lang)
        invite_page = member.get(f"/join/{token}")
        join(member, token)
        mem = member.get("/app/company")
        bad = member.get("/join/" + "q" * 32)
        for r, word in zip((none, own, mem, invite_page), words[lang]):
            assert r.status_code == 200 and f'<html lang="{lang}">' in r.text and word in r.text, (lang, word)
        for r in (none, own, mem, invite_page, bad, owner.get("/app/history"), member.get("/app/plans")):
            assert "{" not in r.text.split("<main>")[1], (lang, r.url)
            assert "<script>" not in r.text and " style=" not in r.text and "onclick" not in r.text


def test_page_has_no_inline_code_and_copy_script_is_safe(env):
    owner = env.login("owner@example.com")
    create(owner)
    r = owner.post("/app/company/invite-link", data={"csrf_token": csrf(owner, "/app/company")})
    assert 'src="/static/company.js"' in r.text and r.headers["cache-control"] == "no-store"
    js = env.new_client().get("/static/company.js").text
    assert "innerHTML" not in js and "eval(" not in js


# ---------- migration ----------

def test_migration_0006_over_existing_data_and_downgrade(tmp_path):
    from alembic import command

    from web.migrate import alembic_config

    url = f"sqlite:///{(tmp_path / 'old.sqlite3').as_posix()}"
    command.upgrade(alembic_config(url), "0005")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("INSERT INTO users (id, email_fp, created_at, trial_pages_left, paid_pages_left, "
                          "trial_granted_at, trial_expires_at, account_code, plan_quota, plan_pages_left) "
                          "VALUES (1, :fp, '2026-10-20 10:00:00', 40, 0, '2026-10-20 10:00:00', "
                          "'2026-11-03 10:00:00', 'SK-AAAAA2', 0, 0)"), {"fp": "1" * 64})
        conn.execute(text("INSERT INTO batches (public_id, user_id, doctype, fmt, one_document, lang, status, "
                          "files_count, files_done, pages_estimated, pages_charged, work_units, docs_found, "
                          "needs_review, failed_files, not_found, created_at, expires_at) VALUES "
                          "(:p, 1, 'receipt', 'xlsx', 0, 'ru', 'failed', 1, 1, 1, 0, 0, 0, 0, 1, 0, "
                          "'2026-10-20 10:00:00', '2027-01-18 10:00:00')"), {"p": "p" * 24})
    eng.dispose()
    command.upgrade(alembic_config(url), "head")
    eng = create_engine(url)
    try:
        assert {"organizations", "memberships", "invitations"} <= set(inspect(eng).get_table_names())
        with eng.connect() as conn:
            assert conn.execute(text("SELECT user_id, org_id, created_by_user_id, wallet_user_id FROM batches")) \
                .all() == [(1, None, 1, 1)]
    finally:
        eng.dispose()
    settings = Settings(db_url=url, secret=SECRET, base_url="http://testserver", dev=True)
    app = create_app(settings, mailer=FakeMailer(), clock=Clock(), executor=InlineExecutor())
    with app.state.sessionmaker() as s:
        u = s.get(User, 1)
        ctx = company.context(s, u)
        assert s.scalar(select(Batch).where(company.batch_scope(ctx))).public_id == "p" * 24
        org = company.create_org(s, u, "Old Co", Clock()())
        assert org is not None and company.member_count(s, org.id) == 1
    app.state.engine.dispose()
    command.downgrade(alembic_config(url), "0005")
    eng = create_engine(url)
    try:
        insp = inspect(eng)
        assert "organizations" not in insp.get_table_names()
        assert "org_id" not in {c["name"] for c in insp.get_columns("batches")}
        with eng.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM batches")).scalar() == 1
    finally:
        eng.dispose()
    command.upgrade(alembic_config(url), "head")
