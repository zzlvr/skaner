"""Separate daily cap for trial-funded work (WEB_TRIAL_DAILY_PAGES): trial farming with many accounts cannot use up
the site-wide daily cap of paying customers. A batch is trial-funded when the paying wallet (the user's own, or the
company owner's) has no plan pages and no top-up pages at upload; any paid page makes it paid-funded."""

import threading

from sqlalchemy import create_engine, inspect, select, text

from test_app import SECRET, Clock, Env, FakeMailer, ManualExecutor, batch_url, latest, png, upload
from test_company import Env as CompanyEnv, team
from test_plans_admin import ADMIN
from web import balance as bal
from web.config import LANGS, Settings
from web.jobs import InlineExecutor, mark_stale
from web.main import create_app
from web.models import Batch, DailyUsage, User

DAY = "2026-10-20"
TRIAL_RU = "Бесплатные пробные страницы на сегодня у всех закончились"


def counters(e, day=DAY):
    with e.db() as s:
        row = s.get(DailyUsage, day)
        return (row.pages, row.trial_pages) if row else (0, 0)


def users(e):
    with e.db() as s:
        return s.scalars(select(User).order_by(User.id)).all()


def give(e, index, topup=0, plan=None):
    with e.db() as s:
        u = s.get(User, users(e)[index].id)
        if topup:
            bal.topup(s, u, topup, None, e.clock())
        if plan:
            bal.grant_plan(s, u, plan, None, e.clock())


def pngs(n):
    return [(f"{i}.png", png()) for i in range(n)]


# ---------- settings ----------

def test_default_and_clamped_setting(monkeypatch):
    base = dict(db_url="sqlite://", secret=SECRET, base_url="http://x", dev=True)
    assert Settings(**base).trial_daily_pages == 100
    assert Settings(**base, daily_pages=50, trial_daily_pages=100).trial_daily_pages == 50
    assert Settings(**base, trial_daily_pages=-5).trial_daily_pages == 0
    monkeypatch.setenv("WEB_DEV", "1")
    monkeypatch.setenv("WEB_BASE_URL", "http://localhost:8000")

    monkeypatch.setenv("WEB_DAILY_PAGES", "300")
    monkeypatch.setenv("WEB_TRIAL_DAILY_PAGES", "500")
    assert Settings.load().trial_daily_pages == 300
    monkeypatch.setenv("WEB_TRIAL_DAILY_PAGES", "40")
    assert Settings.load().trial_daily_pages == 40


# ---------- classification and caps ----------

def test_trial_cap_refuses_trial_work_but_paying_customers_go_on():
    e = Env(daily_pages=10, trial_daily_pages=4, trials_per_ip_day=10)
    t1 = e.login()
    t2 = e.login(e.new_client(), "t2@example.com")
    t3 = e.login(e.new_client(), "t3@example.com")
    paid = e.login(e.new_client(), "paid@example.com")
    planned = e.login(e.new_client(), "plan@example.com")
    give(e, 3, topup=20)
    give(e, 4, plan="start")

    batch_url(upload(t1, pngs(3)))
    assert latest(e).trial_funded and counters(e) == (3, 3)
    r = upload(t2, pngs(2))  # 3 + 2 > 4: only the trial part is full
    assert r.status_code == 503 and TRIAL_RU in r.text
    batch_url(upload(t2, pngs(1)))
    assert counters(e) == (4, 4)
    r = upload(t3, pngs(1))  # refused before the body is read
    assert r.status_code == 503 and TRIAL_RU in r.text
    # Paying customers keep working up to the total cap; the trial counter does not move.
    batch_url(upload(paid, pngs(3)))
    assert not latest(e).trial_funded and counters(e) == (7, 4)
    batch_url(upload(planned, pngs(2)))
    assert not latest(e).trial_funded and counters(e) == (9, 4)
    r = upload(paid, pngs(2))  # the total cap: the old message
    assert r.status_code == 503 and "Сегодня сервис загружен" in r.text and TRIAL_RU not in r.text
    batch_url(upload(paid, pngs(1)))
    assert counters(e) == (10, 4)
    # A new UTC day: both counters start again.
    e.clock.advance(days=1)
    batch_url(upload(t3, pngs(1)))
    assert counters(e, "2026-10-21") == (1, 1)


def test_partly_paid_batch_is_paid_funded():
    e = Env(daily_pages=10, trial_daily_pages=0)
    c = e.login()
    r = upload(c, pngs(1))  # trial-only and the trial part is 0
    assert r.status_code == 503 and TRIAL_RU in r.text
    give(e, 0, topup=1)  # 50 trial pages + 1 top-up page: trial pages are spent first, but it is a paying customer
    batch_url(upload(c, pngs(3)))
    b = latest(e)
    assert not b.trial_funded and b.status == "done" and counters(e) == (3, 0)
    assert bal.balance(users(e)[0], e.clock()).paid_left == 1  # the trial paid for it


def test_company_member_classified_by_the_owners_wallet():
    e = CompanyEnv(trial_daily_pages=5)
    owner, clients = team(e, ("a@example.com",))
    member = clients["a@example.com"]
    batch_url(upload(member, pngs(2)))  # owner has only trial pages
    with e.db() as s:
        b = s.scalars(select(Batch).order_by(Batch.id.desc())).first()
        assert b.trial_funded and b.wallet_user_id == e.user("owner@example.com").id
    assert counters(e) == (2, 2)
    with e.db() as s:
        bal.grant_plan(s, s.get(User, e.user("owner@example.com").id), "start", None, e.clock())
    batch_url(upload(member, pngs(4)))  # owner has a plan now: paid-funded, beyond the trial part
    with e.db() as s:
        assert not s.scalars(select(Batch).order_by(Batch.id.desc())).first().trial_funded
    assert counters(e) == (6, 2)


# ---------- atomic reservation ----------

def test_parallel_reservations_cannot_overshoot_the_trial_cap(tmp_path):
    url = f"sqlite:///{(tmp_path / 'web.sqlite3').as_posix()}"
    e = Env(db_url=url, daily_pages=300, trial_daily_pages=100)
    try:
        s = e.app.state.settings
        barrier, results, lock = threading.Barrier(10), [], threading.Lock()

        def go():
            with e.db() as db:
                barrier.wait()
                code = bal.reserve_daily(db, s, 15, e.clock(), trial=True)
            with lock:
                results.append(code)

        threads = [threading.Thread(target=go) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(None) == 6 and results.count("trial_daily") == 4
        assert counters(e) == (90, 90)
        with e.db() as db:  # sequential: the last 10 trial pages fit, then nothing; paid work still fits
            assert bal.reserve_daily(db, s, 10, e.clock(), trial=True) is None
            assert bal.reserve_daily(db, s, 1, e.clock(), trial=True) == "trial_daily"
            assert bal.reserve_daily(db, s, 200, e.clock()) is None
            assert bal.reserve_daily(db, s, 1, e.clock()) == "daily"
            assert bal.reserve_daily(db, s, 1, e.clock(), trial=True) == "daily"
        assert counters(e) == (300, 100)
    finally:
        e.app.state.engine.dispose()


# ---------- correction after the real cost ----------

def test_correction_adjusts_both_counters():
    e = Env()
    t = e.login()
    p = e.login(e.new_client(), "paid@example.com")
    give(e, 1, topup=50)
    e.plan["dense"] = {"cost": 0.20}  # 3 pages of cost for 1 reserved page (higher than the reservation)
    batch_url(upload(t, [("dense.png", png())]))
    assert counters(e) == (3, 3)
    e.plan["down"] = {"fail": True, "cost": 0}  # reserved 2, cost 1 (lower than the reservation)
    batch_url(upload(t, [("ok.png", png()), ("down.png", png())]))
    assert counters(e) == (4, 4)
    batch_url(upload(p, [("dense.png", png())]))  # paid-funded: only the total moves
    assert counters(e) == (7, 4)
    batch_url(upload(p, [("ok.png", png()), ("down.png", png())]))
    assert counters(e) == (8, 4)


def test_failed_batch_releases_and_restart_keeps_the_reservation():
    e = Env()
    t = e.login()
    e.plan["bad"] = {"fail": True, "cost": 0}
    batch_url(upload(t, [("bad.png", png())]))
    assert latest(e).status == "failed" and counters(e) == (0, 0)

    ex = ManualExecutor()
    e2 = Env(executor=ex)
    t2 = e2.login()
    batch_url(upload(t2, pngs(2)))
    assert counters(e2) == (2, 2)
    assert mark_stale(e2.app.state.jobs) == 1  # interrupted: failed, not charged, reservation stays counted
    ex.jobs.clear()
    assert latest(e2).error_code == "restart" and counters(e2) == (2, 2)


def test_unsubmitted_batch_gives_both_counters_back():
    class Closed:
        def submit(self, *args):
            raise RuntimeError("shut down")

        def shutdown(self, wait=True, cancel_futures=False):
            pass

    e = Env(executor=Closed())
    t = e.login()
    r = upload(t, pngs(2))
    assert r.status_code == 503 and counters(e) == (0, 0)


# ---------- admin and languages ----------

def test_admin_dashboard_shows_trial_pages_today():
    e = Env(admin_emails=(ADMIN,))
    t = e.login()
    batch_url(upload(t, pngs(2)))
    admin = e.login(e.new_client(), ADMIN)
    page = admin.get("/admin").text
    assert "Recognised pages today / cap</th><td>2 / 300" in page
    assert "trial pages today: 2 of 100" in page


def test_trial_cap_message_in_three_languages():
    words = {"ru": TRIAL_RU, "kk": "Бүгінгі тегін сынақ беттері барлық пайдаланушылар үшін таусылды",
             "en": "free trial pages have run out for everyone"}
    e = Env(trial_daily_pages=0, trials_per_ip_day=10)
    for lang in LANGS:
        c = e.login(e.new_client(lang), f"u-{lang}@example.com")
        r = upload(c, pngs(1))
        assert r.status_code == 503 and f'<html lang="{lang}">' in r.text and words[lang] in r.text, lang
        assert 'name="csrf_token"' in r.text  # the usual upload page with the error on it


# ---------- migration ----------

def test_migration_0007_over_existing_data_and_downgrade(tmp_path):
    from alembic import command

    from web.migrate import alembic_config

    url = f"sqlite:///{(tmp_path / 'old.sqlite3').as_posix()}"
    command.upgrade(alembic_config(url), "0006")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email_fp, created_at, trial_pages_left, paid_pages_left, "
                          "trial_granted_at, trial_expires_at, account_code, plan_quota, plan_pages_left) "
                          "VALUES (1, :fp, '2026-10-20 10:00:00', 40, 0, '2026-10-20 10:00:00', "
                          "'2026-11-03 10:00:00', 'SK-AAAAA2', 0, 0)"), {"fp": "1" * 64})
        conn.execute(text("INSERT INTO batches (public_id, user_id, wallet_user_id, created_by_user_id, doctype, "
                          "fmt, one_document, lang, status, files_count, files_done, pages_estimated, pages_charged, "
                          "work_units, docs_found, needs_review, failed_files, not_found, created_at, expires_at) "
                          "VALUES (:p, 1, 1, 1, 'receipt', 'xlsx', 0, 'ru', 'done', 1, 1, 1, 1, 1, 1, 0, 0, 0, "
                          "'2026-10-20 10:00:00', '2027-01-18 10:00:00')"), {"p": "p" * 24})
        conn.execute(text("INSERT INTO daily_usage (day, pages) VALUES ('2026-10-20', 7)"))
    eng.dispose()
    command.upgrade(alembic_config(url), "head")
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            assert conn.execute(text("SELECT pages, trial_pages FROM daily_usage")).all() == [(7, 0)]
            assert conn.execute(text("SELECT trial_funded FROM batches")).scalar() in (0, False)
    finally:
        eng.dispose()
    settings = Settings(db_url=url, secret=SECRET, base_url="http://testserver", dev=True)
    app = create_app(settings, mailer=FakeMailer(), clock=Clock(), executor=InlineExecutor())
    with app.state.sessionmaker() as s:
        assert bal.reserve_daily(s, settings, 3, Clock()(), trial=True) is None
        assert (s.get(DailyUsage, DAY).pages, s.get(DailyUsage, DAY).trial_pages) == (10, 3)
        assert s.scalar(select(Batch)).trial_funded is False
    app.state.engine.dispose()
    command.downgrade(alembic_config(url), "0006")
    eng = create_engine(url)
    try:
        insp = inspect(eng)
        assert "trial_pages" not in {c["name"] for c in insp.get_columns("daily_usage")}
        assert "trial_funded" not in {c["name"] for c in insp.get_columns("batches")}
        with eng.connect() as conn:
            assert conn.execute(text("SELECT pages FROM daily_usage")).scalar() == 10
            assert conn.execute(text("SELECT count(*) FROM batches")).scalar() == 1
    finally:
        eng.dispose()
    command.upgrade(alembic_config(url), "head")
