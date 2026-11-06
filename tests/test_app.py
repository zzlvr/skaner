"""Cabinet: upload, processing, charging, limits, results, downloads, history, privacy and security."""

import csv
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timedelta

import pymupdf
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image
from sqlalchemy import func, inspect, select, text

from web import balance as bal
from web import crypto
from web.config import LANGS, Settings
from web.jobs import InlineExecutor, MemoryBudget
from web.main import create_app
from web.models import Batch, DailyUsage, LedgerEntry, User

SECRET = "test-secret-" + "y" * 40

MERCHANT = "Кофейня Жибек-Секрет"
ITEM = "Капучино-тайный"
BIN = "980740000057"
RECEIPT = {
    "merchant_name": MERCHANT, "merchant_tax_id": BIN, "date": "2026-10-01", "currency": "KZT", "total": 2400,
    "items": [{"description": ITEM, "quantity": 2, "unit_price": 1200, "total": 2400}],
}
BAD_RECEIPT = {  # line total does not match: needs review
    "merchant_name": "Shop", "date": "2026-10-01", "total": 900,
    "items": [{"description": "Bread", "quantity": 2, "unit_price": 300, "total": 900}],
}


# ---------- fakes ----------

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

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))

    def last_code(self):
        return re.search(r"\b(\d{6})\b", self.sent[-1][2]).group(1)


class ExtractionError(RuntimeError):
    """Same class name as the Claude backend's error: mapped to the safe code "service"."""


class FakeBackend:
    """Behaviour chosen by the file name prefix (the engine sees our cleaned name + real type)."""

    name = "fake"

    def __init__(self, plan, log):
        self.plan, self.log = plan, log
        self.cost_usd = 0.0
        self.cache_prefix = False

    def extract(self, parts, schema, instructions):
        names = [p.name for p in parts]
        self.log.append(names)
        rule = next((v for k, v in self.plan.items() if names[0].startswith(k)), {})
        self.cost_usd += rule.get("cost", 0.01)  # 0.01 USD = 1 page by the cost rule
        if rule.get("fail"):
            raise ExtractionError("boom secret text " + MERCHANT)
        docs = rule.get("docs", [RECEIPT])
        if "documents" in schema.model_fields:
            return schema.model_validate({"documents": docs})
        return schema.model_validate(docs[0])


class ManualExecutor:
    """Keeps jobs until run(): lets tests look at queued batches."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class Env:
    def __init__(self, executor=None, db_url="sqlite://", backend_factory=None, **settings):
        self.clock, self.mailer = Clock(), FakeMailer()
        self.plan, self.calls = {}, []
        self.executor = executor or InlineExecutor()
        self.settings = Settings(db_url=db_url, secret=SECRET, base_url="http://testserver", dev=True, **settings)
        self.app = create_app(self.settings, mailer=self.mailer, clock=self.clock, executor=self.executor,
                              backend_factory=backend_factory or (lambda: FakeBackend(self.plan, self.calls)))
        self.client = self.new_client()

    def new_client(self, lang="ru"):
        return TestClient(self.app, headers={"accept-language": lang})

    def db(self):
        return self.app.state.sessionmaker()

    def login(self, client=None, email="anna@example.com"):
        client = client or self.client
        tok = csrf(client, "/login")
        assert client.post("/login", data={"email": email, "csrf_token": tok}, follow_redirects=False).status_code == 303
        r = client.post("/login/code", data={"code": self.mailer.last_code(), "csrf_token": csrf(client, "/")},
                        follow_redirects=False)
        assert r.status_code == 303
        self.clock.advance(seconds=61)  # login code cooldown for the next login in a test
        client.get("/app")  # first cabinet visit: the trial is granted here
        return client

    def user(self, email_index=0):
        with self.db() as s:
            return s.scalars(select(User).order_by(User.id)).all()[email_index]


@pytest.fixture
def env():
    e = Env()
    e.login()
    return e


def csrf(client, path="/app"):
    return re.search(r'name="csrf_token" value="([0-9a-f]{64})"', client.get(path).text).group(1)


# ---------- file fixtures ----------

def png(w=40, h=30, color="white") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


def jpg(w=40, h=30) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(out, "JPEG")
    return out.getvalue()


def pdf(pages=1) -> bytes:
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 72), f"page {n + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def upload(client, files, doctype="receipt", fmt="xlsx", one=False, token=None, headers=None):
    data = {"csrf_token": token or csrf(client), "doctype": doctype, "fmt": fmt}
    if one:
        data["one_document"] = "1"
    parts = [("files", (name, content, "application/octet-stream")) for name, content in files]
    return client.post("/app/upload", data=data, files=parts, follow_redirects=False, headers=headers or {})


def batch_url(r):
    assert r.status_code == 303, r.text[:2000]
    loc = r.headers["location"]
    assert re.fullmatch(r"/app/batches/[A-Za-z0-9_-]{24}", loc)
    return loc


def latest(env) -> Batch:
    with env.db() as s:
        return s.scalar(select(Batch).order_by(Batch.id.desc()))


def batch_url_of(env) -> str:
    return "/app/batches/" + latest(env).public_id


def balance_of(env, index=0) -> int:
    return bal.balance(env.user(index), env.clock()).total


def ledger(env):
    with env.db() as s:
        return s.scalars(select(LedgerEntry).order_by(LedgerEntry.id)).all()


# ---------- access ----------

def test_every_cabinet_route_needs_login():
    e = Env()
    c = e.client
    for path in ("/app", "/app/history", "/account", "/app/batches/" + "a" * 24,
                 "/app/batches/" + "a" * 24 + "/status", "/app/batches/" + "a" * 24 + "/download?fmt=xlsx"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/"), path
    tok = csrf(c, "/")
    r = upload(c, [("a.png", png())], token=tok)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = c.post("/app/batches/" + "a" * 24 + "/delete", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert e.calls == []


def header(html):
    return html[html.index('<header class="top">'):html.index("</header>")]


def test_signed_in_header_shows_cabinet(env):
    for path in ("/", "/app", "/privacy"):
        top = header(env.client.get(path).text)
        assert 'href="/app">Кабинет' in top and "Попробовать" not in top and "Войти" not in top, path
    anon = header(env.new_client().get("/").text)
    assert 'href="/login">Попробовать' in anon and 'href="/login">Войти' in anon and "Кабинет" not in anon


# ---------- happy path ----------

def test_upload_process_result_and_every_download(env):
    c = env.client
    page = c.get("/app")
    assert page.status_code == 200 and "50" in page.text and 'capture="environment"' in page.text
    assert page.headers["cache-control"] == "no-store"
    url = batch_url(upload(c, [("чек 1.png", png()), ("scan.pdf", pdf(1))]))
    r = c.get(url)
    assert r.status_code == 200 and "Готово" in r.text and MERCHANT in r.text and "чек 1" in r.text
    assert balance_of(env) == 48
    b = latest(env)
    assert (b.status, b.files_count, b.pages_estimated, b.pages_charged, b.docs_found) == ("done", 2, 2, 2, 2)
    assert env.app.state.memory.used == 0  # memory released after the job

    media = {"xlsx": "spreadsheetml", "1c": "spreadsheetml", "csv": "application/zip", "json": "application/json"}
    for fmt, kind in media.items():
        d = c.get(f"{url}/download?fmt={fmt}")
        assert d.status_code == 200 and kind in d.headers["content-type"], fmt
        assert d.headers["content-disposition"].startswith('attachment; filename="skaner_')
        assert d.headers["cache-control"] == "no-store" and d.headers["x-content-type-options"] == "nosniff"
        if fmt in ("xlsx", "1c"):
            wb = load_workbook(io.BytesIO(d.content))
            assert any(MERCHANT in str(cell.value) for ws in wb.worksheets for row in ws.iter_rows() for cell in row)
        elif fmt == "csv":
            with zipfile.ZipFile(io.BytesIO(d.content)) as z:
                assert set(z.namelist()) == {"documents.csv", "items.csv"}
                assert MERCHANT in z.read("documents.csv").decode("utf-8-sig")
        else:
            rows = json.loads(d.content)
            assert rows[0]["data"]["merchant_name"] == MERCHANT
            assert all(r["model"] == "" for r in rows)  # the provider is not exposed
    assert c.get(f"{url}/download?fmt=exe").status_code == 404

    hist = c.get("/app/history")
    assert hist.status_code == 200 and url in hist.text and "Кассовые чеки" in hist.text


def test_status_endpoint_and_progress_page():
    e = Env(executor=ManualExecutor())
    e.login()
    url = batch_url(upload(e.client, [("a.png", png()), ("b.png", png())]))
    page = e.client.get(url)
    assert "В очереди" in page.text and 'id="bstat"' in page.text and "/static/upload.js" in page.text
    assert '<meta http-equiv="refresh"' in page.text  # inside <noscript>: works without JS
    s = e.client.get(url + "/status").json()
    assert s == {"status": "queued", "done": 0, "total": 2, "label": "В очереди"}
    assert e.client.get(url + "/download?fmt=xlsx").status_code == 404  # not ready
    e.executor.run()
    assert e.client.get(url + "/status").json()["status"] == "done"
    assert "Готово" in e.client.get(url).text


def test_review_fields_highlighted(env):
    env.plan["bad"] = {"docs": [BAD_RECEIPT]}
    r = env.client.get(batch_url(upload(env.client, [("bad.png", png())])))
    assert 'class="flag"' in r.text and "Проверьте" in r.text and "st warn" in r.text
    assert latest(env).needs_review == 1


def test_long_receipt_is_one_document(env):
    url = batch_url(upload(env.client, [(f"part{i}.jpg", jpg()) for i in range(3)], one=True))
    assert env.calls == [["part0.jpg", "part1.jpg", "part2.jpg"]]
    b = latest(env)
    assert (b.docs_found, b.pages_charged) == (1, 3) and env.client.get(url).status_code == 200
    r = upload(env.client, [(f"p{i}.jpg", jpg()) for i in range(11)], one=True)
    assert r.status_code == 400 and "не больше 10" in r.text


# ---------- charging ----------

def test_page_rule_counts_documents_and_pdf_pages(env):
    env.plan["two"] = {"docs": [RECEIPT, RECEIPT]}
    batch_url(upload(env.client, [("two.png", png())]))
    assert latest(env).pages_charged == 2 and latest(env).docs_found == 2
    batch_url(upload(env.client, [("long.pdf", pdf(3))]))
    assert latest(env).pages_charged == 3
    assert balance_of(env) == 45
    rows = [r for r in ledger(env) if r.reason == "batch"]
    assert [(r.charged, r.trial_delta) for r in rows] == [(2, -2), (3, -3)]
    assert all(r.batch_ref for r in rows)


def test_cost_floor(env):
    env.plan["dense"] = {"cost": 0.20}  # 0.20 USD * 470 / 45 = 2.09 -> 3 pages
    batch_url(upload(env.client, [("dense.png", png())]))
    assert latest(env).pages_charged == 3 and balance_of(env) == 47


def test_failed_job_without_cost_is_free(env):
    env.plan["down"] = {"fail": True, "cost": 0}
    url = batch_url(upload(env.client, [("down.png", png())]))
    b = latest(env)
    assert (b.status, b.error_code, b.pages_charged) == ("failed", "service", 0)
    assert balance_of(env) == 50
    page = env.client.get(url).text
    assert "Не удалось обработать" in page and "boom" not in page


def test_free_failures_per_day_then_charged(env):
    env.plan["bad"] = {"fail": True, "cost": 0.01}
    for _ in range(3):
        batch_url(upload(env.client, [("bad.png", png())]))
        assert latest(env).pages_charged == 0
    batch_url(upload(env.client, [("bad.png", png())]))
    assert latest(env).pages_charged == 1 and balance_of(env) == 49
    env.clock.advance(days=1)
    batch_url(upload(env.client, [("bad.png", png())]))
    assert latest(env).pages_charged == 0  # a new day: free again


def test_partial_failure_charges_the_good_files(env):
    env.plan["bad"] = {"fail": True, "cost": 0}
    url = batch_url(upload(env.client, [("ok.png", png()), ("bad.png", png())]))
    b = latest(env)
    assert (b.status, b.pages_charged, b.failed_files, b.docs_found) == ("done", 1, 1, 1)
    assert "Не обработан" in env.client.get(url).text


def test_refused_when_balance_too_small(env):
    with env.db() as s:
        s.get(User, env.user().id).trial_pages_left = 2
        s.commit()
    r = upload(env.client, [("long.pdf", pdf(3))])
    assert r.status_code == 402 and "нужно 3, осталось 2" in r.text
    assert env.calls == [] and latest(env) is None


def test_overrun_never_below_zero_and_recorded(env):
    with env.db() as s:
        s.get(User, env.user().id).trial_pages_left = 1
        s.commit()
    env.plan["dense"] = {"cost": 0.20}
    batch_url(upload(env.client, [("dense.png", png())]))
    u = env.user()
    assert u.trial_pages_left == 0 and balance_of(env) == 0
    row = ledger(env)[-1]
    assert (row.charged, row.trial_delta, row.overrun) == (3, -1, 2)


def test_trial_expires_by_clock(env):
    assert balance_of(env) == 50
    env.clock.advance(days=14, seconds=1)
    assert balance_of(env) == 0
    r = upload(env.client, [("a.png", png())])
    assert r.status_code == 402 and env.calls == []
    assert "Пробный период закончился" in env.client.get("/app").text


def test_one_trial_per_user(env):
    batch_url(upload(env.client, [("a.png", png())]))
    env.client.post("/logout", data={"csrf_token": csrf(env.client)})
    env.login()
    env.client.get("/app")
    with env.db() as s:
        u = s.scalar(select(User))
        assert not bal.ensure_trial(s, env.settings, u, "ip", env.clock())
    grants = [r for r in ledger(env) if r.reason == "trial_grant"]
    assert len(grants) == 1 and grants[0].trial_delta == 50 and balance_of(env) == 49


def test_paid_pages_used_after_trial(env):
    with env.db() as s:
        u = s.get(User, env.user().id)
        u.trial_pages_left, u.paid_pages_left = 1, 5
        s.commit()
    batch_url(upload(env.client, [("long.pdf", pdf(3))]))
    u = env.user()
    assert (u.trial_pages_left, u.paid_pages_left) == (0, 3)
    assert (ledger(env)[-1].trial_delta, ledger(env)[-1].paid_delta) == (-1, -2)


def test_backend_unavailable_is_free():
    def broken():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    e = Env(backend_factory=broken)
    e.login()
    batch_url(upload(e.client, [("a.png", png())]))
    b = latest(e)
    assert (b.status, b.error_code, b.pages_charged) == ("failed", "service", 0) and balance_of(e) == 50


# ---------- limits ----------

@pytest.mark.no_trial
def test_default_limits():
    s = Settings(db_url="sqlite://", secret=SECRET, base_url="http://x", dev=True)
    mb = 1024 * 1024
    assert (s.daily_pages, s.batches_per_hour, s.max_files, s.max_file_bytes, s.max_pdf_pages) == (300, 20, 20, 20 * mb, 20)
    assert (s.max_batch_bytes, s.memory_total, s.memory_user, s.workers) == (60 * mb, 400 * mb, 100 * mb, 2)
    assert (s.trial_pages, s.trial_days, s.trials_per_ip_day, s.retention_days, s.model) == (0, 14, 3, 90, "claude-opus-5-5")


def test_too_many_files(env):
    r = upload(env.client, [(f"{i}.png", png()) for i in range(21)])
    assert r.status_code == 413 and "Не больше 20 файлов" in r.text and env.calls == []


def test_file_too_large():
    e = Env(max_file_bytes=5000)
    e.login()
    big = png() + b"\0" * 6000
    r = upload(e.client, [("small.png", png()), ("huge.png", big)])
    assert r.status_code == 413 and "Файл 2 (huge)" in r.text and "больше" in r.text
    assert e.calls == [] and latest(e) is None and e.app.state.memory.used == 0


def test_total_size_limits():
    e = Env(max_batch_bytes=20_000)
    e.login()
    r = upload(e.client, [(f"{i}.png", png() + b"\0" * 8000) for i in range(3)])
    assert r.status_code == 413 and "Слишком большой объём" in r.text and e.calls == []
    huge = b"\0" * (1024 * 1024 + 30_000)  # above the limit plus form overhead: refused before reading
    r = upload(e.client, [("x.png", huge)])
    assert r.status_code == 413 and e.app.state.memory.used == 0


def test_pdf_page_limit(env):
    r = upload(env.client, [("ok.png", png()), ("book.pdf", pdf(21))])
    assert r.status_code == 400 and "Файл 2 (book)" in r.text and "больше 20 страниц" in r.text
    assert env.calls == []


def test_memory_cap_per_user():
    e = Env(memory_user=10_000)
    e.login()
    r = upload(e.client, [("a.png", png() + b"\0" * 20_000)])
    assert r.status_code == 503 and "занят" in r.text and e.app.state.memory.used == 0


def test_memory_budget_unit():
    m = MemoryBudget(total=100, per_user=60)
    a = m.reserve(1, 60)
    assert a and m.reserve(1, 1) is None and m.reserve(2, 50) is None  # per user, then total
    b = m.reserve(2, 40)
    a.release()
    a.release()  # idempotent
    assert m.used == 40 and m.reserve(1, 60)
    b.release()


def test_one_running_batch_per_user():
    e = Env(executor=ManualExecutor())
    e.login()
    batch_url(upload(e.client, [("a.png", png())]))
    r = upload(e.client, [("b.png", png())])
    assert r.status_code == 409 and "ещё обрабатывается" in r.text
    assert "ещё обрабатывается" in e.client.get("/app").text
    e.executor.run()
    batch_url(upload(e.client, [("b.png", png())]))


def test_hourly_batches_per_user():
    e = Env(batches_per_hour=2)
    e.login()
    for _ in range(2):
        batch_url(upload(e.client, [("a.png", png())]))
    url = latest(e).public_id
    with e.db() as s:  # deleting a batch must not free a slot
        s.delete(s.scalar(select(Batch).where(Batch.public_id == url)))
        s.commit()
    r = upload(e.client, [("a.png", png())])
    assert r.status_code == 429 and "Попробуйте позже" in r.text
    e.clock.advance(hours=1, seconds=1)
    batch_url(upload(e.client, [("a.png", png())]))


def test_daily_global_cap():
    e = Env(daily_pages=3)
    e.login()
    batch_url(upload(e.client, [("a.png", png()), ("b.png", png())]))
    other = e.login(e.new_client(), "boris@example.com")
    r = upload(other, [("long.pdf", pdf(2))])
    assert r.status_code == 503 and "Попробуйте позже" in r.text and len(e.calls) == 2
    batch_url(upload(other, [("c.png", png())]))
    with e.db() as s:
        assert s.get(DailyUsage, "2026-10-20").pages == 3
    e.clock.advance(days=1)
    batch_url(upload(other, [("long.pdf", pdf(2))]))


def test_new_trial_per_ip_cap():
    e = Env()
    clients = [e.login(e.new_client(), f"user{i}@example.com") for i in range(4)]
    for c in clients:
        c.get("/app")
    assert [balance_of(e, i) for i in range(4)] == [50, 50, 50, 0]
    page = clients[3].get("/app").text
    assert "с этой сети сегодня недоступен" in page
    r = upload(clients[3], [("a.png", png())])
    assert r.status_code == 402 and e.calls == []
    e.clock.advance(days=1, seconds=1)
    clients[3].get("/app")
    assert balance_of(e, 3) == 50


# ---------- content validation ----------

def rejected(env, name, data, words, status=400):
    r = upload(env.client, [(name, data)])
    assert r.status_code == status and words in r.text, r.text[-3000:]
    assert env.calls == [] and latest(env) is None


def test_fake_extension_is_judged_by_content(env):
    rejected(env, "virus.jpg", b"MZ\x90\x00 this is not an image", "формат не поддерживается")
    batch_url(upload(env.client, [("really_png.pdf", png())]))  # a PNG named .pdf is just a PNG
    assert env.calls == [["really_png.png"]] and latest(env).pages_estimated == 1


def test_truncated_images(env):
    rejected(env, "cut.png", png(200, 200, "red")[:-30], "повреждён")
    rejected(env, "cut.jpg", jpg(400, 300)[:300], "повреждён")


def test_decompression_bomb(env):
    out = io.BytesIO()
    Image.new("1", (8000, 8000)).save(out, "PNG")  # tiny file, 64 megapixels
    rejected(env, "bomb.png", out.getvalue(), "слишком большое изображение")


def test_non_pdf_named_pdf(env):
    rejected(env, "doc.pdf", b"hello, I am text", "формат не поддерживается")
    rejected(env, "doc2.pdf", b"%PDF-1.7\n garbage garbage", "PDF не читается")


def test_heic_and_empty(env):
    rejected(env, "photo.heic", b"\0\0\0\x18ftypheic\0\0\0\0mif1heic" + b"\0" * 100, "HEIC")
    r = env.client.post("/app/upload", data={"csrf_token": csrf(env.client), "doctype": "receipt", "fmt": "xlsx"},
                        files=[("files", ("", b"", "application/octet-stream"))], follow_redirects=False)
    assert r.status_code == 400 and "Выберите хотя бы один файл" in r.text


def test_bad_choices_rejected(env):
    for doctype, fmt in (("passport", "xlsx"), ("receipt", "exe"), ("../x", "1c")):
        r = upload(env.client, [("a.png", png())], doctype=doctype, fmt=fmt)
        assert r.status_code == 400
    assert env.calls == []


# ---------- ownership ----------

def test_foreign_and_unknown_batches_are_404(env):
    url = batch_url(upload(env.client, [("a.png", png())]))
    other = env.login(env.new_client(), "eve@example.com")
    for path in (url, url + "/status", url + "/download?fmt=xlsx", "/app/batches/" + "x" * 24, "/app/batches/short"):
        assert other.get(path).status_code == 404, path
    r = other.post(url + "/delete", data={"csrf_token": csrf(other)}, follow_redirects=False)
    assert r.status_code == 404 and latest(env) is not None
    assert url not in other.get("/app/history").text


def test_batch_ids_are_random(env):
    ids = {batch_url(upload(env.client, [("a.png", png())])).rsplit("/", 1)[1] for _ in range(3)}
    assert len(ids) == 3 and all(len(i) == 24 for i in ids)
    with env.db() as s:
        assert [b.id for b in s.scalars(select(Batch))] == [1, 2, 3]  # internal ids never appear in URLs


# ---------- CSRF ----------

def test_csrf_on_every_post(env):
    c = env.client
    assert upload(c, [("a.png", png())], token="0" * 64).status_code == 403
    foreign = csrf(env.new_client(), "/")
    assert upload(c, [("a.png", png())], token=foreign).status_code == 403
    r = upload(c, [("a.png", png())], headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    # Files before the token: refused before any file byte is accepted.
    body = (b"--B\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.png\"\r\n\r\n" + png() +
            b"\r\n--B\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n" + csrf(c).encode() +
            b"\r\n--B--\r\n")
    r = c.post("/app/upload", content=body, headers={"content-type": "multipart/form-data; boundary=B"},
               follow_redirects=False)
    assert r.status_code == 403
    assert env.calls == [] and latest(env) is None
    url = batch_url(upload(c, [("a.png", png())]))
    assert c.post(url + "/delete", data={}, follow_redirects=False).status_code == 403
    assert c.post(url + "/delete", data={"csrf_token": foreign}, follow_redirects=False).status_code == 403
    assert latest(env) is not None
    assert c.get(url + "/delete").status_code == 405  # no state change by GET


# ---------- data at rest ----------

def all_values(app):
    out = []
    with app.state.engine.connect() as conn:
        for table in inspect(conn).get_table_names():
            for row in conn.execute(text(f'SELECT * FROM "{table}"')):
                out += [v for v in row if v is not None]
    return out


def test_no_plaintext_results_or_names_in_database(env):
    env.plan["secret"] = {"docs": [RECEIPT]}
    url = batch_url(upload(env.client, [("secret-invoice-name.png", png())]))
    assert MERCHANT in env.client.get(url).text
    values = all_values(env.app)
    for needle in (MERCHANT, ITEM, BIN, "secret-invoice-name", "anna"):
        for v in values:
            if isinstance(v, bytes):
                assert needle.encode("utf-8") not in v
            else:
                assert needle not in str(v)
    with env.db() as s:
        b = s.scalar(select(Batch))
        payload = crypto.decrypt(env.settings, b.public_id, b.results_enc)
    assert payload["results"][0]["data"]["merchant_name"] == MERCHANT
    assert payload["results"][0]["file"] == "secret-invoice-name.png"


def test_tampered_or_moved_results_are_rejected(env):
    batch_url(upload(env.client, [("a.png", png())]))
    url = batch_url(upload(env.client, [("b.png", png())]))
    with env.db() as s:
        first, second = s.scalars(select(Batch).order_by(Batch.id)).all()
        with pytest.raises(crypto.ResultsUnreadable):
            crypto.decrypt(env.settings, second.public_id, first.results_enc)  # bound to its batch
        blob = bytearray(second.results_enc)
        blob[-5] ^= 1
        second.results_enc = bytes(blob)
        s.commit()
        with pytest.raises(crypto.ResultsUnreadable):
            crypto.decrypt(env.settings, second.public_id, second.results_enc)
    page = env.client.get(url)
    assert page.status_code == 200 and "Результаты недоступны" in page.text
    assert env.client.get(url + "/download?fmt=xlsx").status_code == 404
    other_key = Settings(db_url="sqlite://", secret="z" * 50, base_url="http://x", dev=True)
    with pytest.raises(crypto.ResultsUnreadable):
        crypto.decrypt(other_key, first.public_id, first.results_enc)


def test_retention_purge_after_90_days(env):
    url = batch_url(upload(env.client, [("a.png", png())]))
    env.clock.advance(days=89)
    c = env.login(env.new_client())  # the old session has expired by now; same account
    assert c.get(url, follow_redirects=False).status_code == 200 and latest(env) is not None
    env.clock.advance(days=1, seconds=1)
    other = env.login(env.new_client(), "someone@example.com")  # any cabinet visit purges expired results
    other.get("/app/history")
    assert latest(env) is None and c.get(url, follow_redirects=False).status_code == 404
    assert any(r.reason == "batch" for r in ledger(env))  # the charge record stays


def test_delete_removes_the_batch(env):
    url = batch_url(upload(env.client, [("a.png", png())]))
    r = env.client.post(url + "/delete", data={"csrf_token": csrf(env.client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/history"
    assert latest(env) is None and env.client.get(url).status_code == 404
    with env.db() as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.reason == "batch")) == 1


def test_running_batch_cannot_be_deleted():
    e = Env(executor=ManualExecutor())
    e.login()
    url = batch_url(upload(e.client, [("a.png", png())]))
    assert 'action="' + url + '/delete"' not in e.client.get(url).text
    r = e.client.post(url + "/delete", data={"csrf_token": csrf(e.client)}, follow_redirects=False)
    assert r.status_code == 409 and latest(e) is not None


def test_restart_marks_stuck_jobs_failed_without_charge(tmp_path):
    url_db = f"sqlite:///{(tmp_path / 'web.sqlite3').as_posix()}"
    e = Env(executor=ManualExecutor(), db_url=url_db)
    e.login()
    url = batch_url(upload(e.client, [("a.png", png())]))
    assert latest(e).status == "queued"
    e.app.state.engine.dispose()
    e2 = Env(db_url=url_db)  # a new process on the same database
    try:
        b = latest(e2)
        assert (b.status, b.error_code, b.pages_charged) == ("failed", "restart", 0)
        assert balance_of(e2) == 50 and not [r for r in ledger(e2) if r.reason == "batch"]
        e2.client.cookies.update(e.client.cookies)
        assert "перезапуска сервера" in e2.client.get(url).text
        e.executor.run()  # the old process's job cannot run any more
        assert latest(e2).status == "failed" and balance_of(e2) == 50
    finally:
        e2.app.state.engine.dispose()


def test_real_thread_pool(tmp_path):
    """The production path: a background pool and a file database (no sleeping: shutdown waits for the job)."""
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=2)
    e = Env(executor=pool, db_url=f"sqlite:///{(tmp_path / 'p.sqlite3').as_posix()}")
    try:
        e.login()
        url = batch_url(upload(e.client, [("a.png", png()), ("b.pdf", pdf(2))]))
        pool.shutdown(wait=True)
        b = latest(e)
        assert (b.status, b.pages_charged, b.docs_found) == ("done", 3, 2)
        assert "Готово" in e.client.get(url).text and e.app.state.memory.used == 0
    finally:
        e.app.state.engine.dispose()


def test_upload_needs_content_length(env):
    def body():
        yield b"--B\r\n"

    r = env.client.post("/app/upload", content=body(), headers={"content-type": "multipart/form-data; boundary=B"},
                        follow_redirects=False)
    assert r.status_code == 411


def test_migration_creates_new_tables(tmp_path):
    url_db = f"sqlite:///{(tmp_path / 'm.sqlite3').as_posix()}"
    e = Env(db_url=url_db)
    try:
        with e.app.state.engine.connect() as conn:
            assert {"batches", "ledger", "daily_usage"} <= set(inspect(conn).get_table_names())
    finally:
        e.app.state.engine.dispose()


# ---------- logs ----------

def test_logs_hold_no_document_data(caplog):
    caplog.set_level(logging.DEBUG)
    e = Env()
    e.login()
    e.plan["boom"] = {"fail": True, "cost": 0.01}
    url = batch_url(upload(e.client, [("private-name.png", png()), ("boom-file.png", png())]))
    e.client.get(url + "/download?fmt=xlsx")
    upload(e.client, [("rejected-name.jpg", b"not an image")])
    session = e.client.cookies.get("sk_session")
    messages = [r.getMessage() + " " + repr(r.args) for r in caplog.records]
    assert any("batch finished" in m for m in messages)
    for m in messages:
        for secret in (MERCHANT, ITEM, BIN, "private-name", "boom-file", "rejected-name", "boom secret", "anna",
                       session):
            assert secret not in m, m


# ---------- languages and escaping ----------

def test_cabinet_pages_in_three_languages():
    e = Env()
    words = {
        "ru": ("Загрузка документов", "Документы", "Готово", "Вы вошли"),
        "kk": ("Құжаттарды жүктеу", "Құжаттар", "Дайын", "Сіз аккаунтқа кірдіңіз"),
        "en": ("Upload documents", "Documents", "Done", "You are signed in"),
    }
    for lang in LANGS:
        c = e.login(e.new_client(lang), f"{lang}@example.com")
        url = batch_url(upload(c, [("a.png", png())]))
        for path, word in zip(("/app", "/app/history", url, "/account"), words[lang]):
            r = c.get(path)
            assert r.status_code == 200 and f'<html lang="{lang}">' in r.text and word in r.text, (lang, path)
        assert latest(e).lang == lang


def test_malicious_values_and_names_are_escaped(env):
    evil = dict(RECEIPT, merchant_name='<script>alert(1)</script>', receipt_number='"><img src=x onerror=alert(2)>')
    env.plan["x"] = {"docs": [evil]}
    url = batch_url(upload(env.client, [('x<img src=y onerror=alert(3)>.png', png())]))
    r = env.client.get(url)
    assert "<script>alert" not in r.text and "<img src=" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text and "x&lt;img src=y onerror=alert(3)&gt;" in r.text
    # A path in the file name is dropped: only the last part is kept.
    batch_url(upload(env.client, [("../../etc/x<script>.png", png())]))
    assert "../" not in env.client.get(batch_url_of(env)).text.split('<div class="docs">')[1]
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp and "unsafe-inline" not in csp
    assert "<script>" not in r.text and " style=" not in r.text and " onclick" not in r.text
    bad = upload(env.client, [("<b onmouseover=x>bold.jpg", b"text")])
    assert "<b onmouseover" not in bad.text and "&lt;b onmouseover=x&gt;bold" in bad.text


def test_formula_injection_exported_as_text(env):
    formula = '=HYPERLINK("http://evil.example","click")'
    env.plan["f"] = {"docs": [dict(RECEIPT, merchant_name=formula, items=[
        {"description": "@SUM(A1:A9)", "quantity": 1, "unit_price": 1, "total": 1}])]}
    url = batch_url(upload(env.client, [("f.png", png())]))
    for fmt in ("xlsx", "1c"):
        wb = load_workbook(io.BytesIO(env.client.get(f"{url}/download?fmt={fmt}").content))
        cells = [cell for ws in wb.worksheets for row in ws.iter_rows() for cell in row if cell.value == formula]
        assert cells and all(cell.data_type == "s" for cell in cells), fmt
    with zipfile.ZipFile(io.BytesIO(env.client.get(f"{url}/download?fmt=csv").content)) as z:
        docs = list(csv.DictReader(io.StringIO(z.read("documents.csv").decode("utf-8-sig")), delimiter=";"))
        items = list(csv.DictReader(io.StringIO(z.read("items.csv").decode("utf-8-sig")), delimiter=";"))
    assert docs[0]["merchant_name"] == "'" + formula and items[0]["description"] == "'@SUM(A1:A9)"


def test_csv_opens_in_russian_excel(env):
    """QA 2026-10-25: Excel in ru/kk Windows splits CSV by ";" and reads "," as the decimal sign. A comma-separated
    file landed in one column there, and 2400.0 / 12.5 were text or dates. English files keep "," and "."."""
    env.plan["c"] = {"docs": [dict(RECEIPT, total=2400.5, items=[
        {"description": "Сыр, весовой", "quantity": 1.25, "unit_price": 1920.4, "total": 2400.5},
        {"description": "Возврат тары", "quantity": -1, "unit_price": 100, "total": -100}])]}
    url = batch_url(upload(env.client, [("c.png", png())]))
    with zipfile.ZipFile(io.BytesIO(env.client.get(f"{url}/download?fmt=csv").content)) as z:
        text = z.read("items.csv").decode("utf-8-sig")
    assert text.splitlines()[0] == "file;line;description;quantity;unit_price;total"
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    assert (rows[0]["description"], rows[0]["quantity"], rows[0]["unit_price"], rows[0]["total"]) == \
        ("Сыр, весовой", "1,25", "1920,4", "2400,5")
    assert (rows[1]["quantity"], rows[1]["total"]) == ("-1", "-100")  # numbers, not "'-1" text

    en = env.new_client("en")
    env.login(en, email="bob@example.com")
    env.plan["e"] = env.plan["c"]
    url = batch_url(upload(en, [("e.png", png())]))
    with zipfile.ZipFile(io.BytesIO(en.get(f"{url}/download?fmt=csv").content)) as z:
        rows = list(csv.DictReader(io.StringIO(z.read("items.csv").decode("utf-8-sig"))))
    assert (rows[0]["quantity"], rows[0]["total"]) == ("1.25", "2400.5")


def test_upload_page_has_no_inline_code(env):
    html = env.client.get("/app").text
    assert "<script>" not in html and " style=" not in html and "onclick" not in html
    assert 'enctype="multipart/form-data"' in html and 'accept="image/*" capture="environment"' in html
    assert html.index('name="csrf_token"', html.index('id="upform"')) < html.index('name="doctype"')
    js = env.client.get("/static/upload.js").text
    assert "innerHTML" not in js and "eval(" not in js
