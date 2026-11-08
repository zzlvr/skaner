"""Cabinet: the two PDF formats (free "PDF-скан" and charged "PDF с текстом")."""

import io
import json
import logging
import re
from pathlib import Path

import pymupdf
import pytest
from markupsafe import escape
from PIL import Image, ImageDraw
from sqlalchemy import inspect, select, text

from test_app import (  # helpers only; no test functions are imported
    MERCHANT, SECRET, Clock, Env, FakeMailer, ManualExecutor, all_values, balance_of, batch_url, csrf, jpg, latest,
    ledger, pdf, png, upload,
)
from web import crypto
from web.config import LANGS, Settings
from web.jobs import InlineExecutor
from web.main import create_app
from web.models import Batch, ScanDaily, ScanUsage, User

LOCALES = Path(__file__).parent.parent / "web" / "locales"
LOC = {lang: json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8")) for lang in LANGS}

SECRET_TEXT = "Договор-тайный-4815"
PAGE = {"title": "Договор поставки", "html": f"<h1>Договор поставки</h1><p>Сторона: {SECRET_TEXT}</p><p>[подпись]</p>"}


def photo(w=600, h=800) -> bytes:
    """A grey "table" with a white sheet and a few dark lines: something for the scanner to find."""
    im = Image.new("RGB", (w, h), (90, 90, 90))
    d = ImageDraw.Draw(im)
    d.rectangle((w // 8, h // 8, w * 7 // 8, h * 7 // 8), fill="white")
    for y in range(h // 4, h * 3 // 4, 40):
        d.line((w // 5, y, w * 4 // 5, y), fill="black", width=3)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


def scan_up(client, files, **kw):
    return upload(client, files, fmt="pdf_scan", **kw)


def text_up(client, files, **kw):
    return upload(client, files, fmt="pdf_text", **kw)


def pdf_pages(content: bytes) -> list[str]:
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        return [page.get_text() for page in doc]


class NoBackend:
    """Fails the test if the recognition backend is ever created."""

    def __init__(self):
        self.created = 0

    def __call__(self):
        self.created += 1
        raise AssertionError("a scan must not use the recognition backend")


@pytest.fixture
def env():
    e = Env()
    e.login()
    return e


# ---------- upload form ----------

def test_form_has_both_pdf_formats_and_legend_in_three_languages():
    e = Env()
    for lang in LANGS:
        c = e.login(e.new_client(lang), f"{lang}@example.com")
        html = c.get("/app").text
        t = LOC[lang]
        assert f'<optgroup label="{t["up_fmt_tables"]}">' in html and '<optgroup label="PDF">' in html
        for key in ("xlsx", "1c", "csv", "json", "pdf_text"):
            assert f'<option value="{key}" data-kind=' in html, (lang, key)
        assert str(escape(t["fmt_pdf_text"])) in html and 'value="pdf_scan"' not in html
        assert 'id="updoctype"' in html and 'id="fmtdesc"' in html
        assert "<script>" not in html and " style=" not in html and "onclick" not in html
    js = e.client.get("/static/upload.js").text
    assert "data-kind" in js and "updoctype" in js and "innerHTML" not in js


def test_landing_page_shows_only_the_text_pdf_chip():
    e = Env()
    for lang in LANGS:
        html = e.new_client(lang).get("/").text
        chips = re.search(r'<div class="chips"><span>Excel</span>.*?</div>', html).group(0)
        assert f"<span>JSON</span><span>{LOC[lang]['chip_pdf_text']}</span>" in chips and LOC[lang]['chip_pdf_scan'] not in chips
        assert "<span>PDF</span>" not in chips


def test_new_keys_in_every_locale():
    keys = {lang: set(d) for lang, d in LOC.items()}
    assert keys["ru"] == keys["kk"] == keys["en"]
    for k in ("fmt_pdf_scan", "fmt_pdf_text", "fd_scan", "b_file_gone", "err_scan_daily", "chip_pdf_text"):
        assert all(LOC[lang][k] for lang in LANGS)


# ---------- PDF scan: not offered any more ----------

def test_scan_is_not_offered_and_is_refused(env):
    c = env.client
    html = c.get("/app").text
    assert '<option value="pdf_scan"' not in html and 'id="scanleft"' not in html
    r = scan_up(c, [("a.jpg", photo())])
    assert r.status_code == 400 and latest(env) is None


# ---------- PDF с текстом ----------

def test_text_pdf_charged_per_page_and_text_is_selectable(env):
    env.plan["doc"] = {"docs": [PAGE]}
    c = env.client
    url = batch_url(text_up(c, [("doc1.png", png()), ("doc2.pdf", pdf(2))], doctype="../x", one=True))
    assert env.calls == [["doc1.png"], ["doc2.pdf p1"], ["doc2.pdf p2"]]  # page by page, "one document" ignored
    b = latest(env)
    assert (b.status, b.fmt, b.doctype, b.pages_charged, b.docs_found, b.failed_files) == \
        ("done", "pdf_text", "", 3, 3, 0)
    assert balance_of(env) == 47
    row = ledger(env)[-1]
    assert (row.reason, row.charged, row.trial_delta) == ("batch", 3, -3)

    page = c.get(url).text
    assert LOC["ru"]["b_text_what"].replace("{n}", "3") in page and "Списано страниц: 3" in page
    assert page.count("/download?fmt=") == 1 and "Результаты хранятся 90 дней" in page
    d = c.get(url + "/download?fmt=pdf_text")
    assert d.status_code == 200 and d.headers["content-type"] == "application/pdf"
    assert re.fullmatch(r'attachment; filename="skaner_20261020_12\d\d_text\.pdf"', d.headers["content-disposition"])
    assert d.headers["cache-control"] == "no-store" and d.headers["x-content-type-options"] == "nosniff"
    texts = pdf_pages(d.content)
    assert len(texts) == 3 and all(SECRET_TEXT in t and "Договор поставки" in t for t in texts)
    assert "PDF с текстом" in c.get("/app/history").text
    assert c.get(url + "/download?fmt=xlsx").status_code == 404
    assert c.get(url + "/download?fmt=pdf_scan").status_code == 404


def test_text_pdf_cost_floor(env):
    env.plan["dense"] = {"docs": [PAGE], "cost": 0.20}  # 0.20 USD * 470 / 45 = 2.09 -> 3 pages
    batch_url(text_up(env.client, [("dense.png", png())]))
    assert latest(env).pages_charged == 3 and balance_of(env) == 47


def test_text_pdf_refused_when_balance_too_small(env):
    with env.db() as s:
        s.get(User, env.user().id).trial_pages_left = 2
        s.commit()
    r = text_up(env.client, [("doc.pdf", pdf(3))])
    assert r.status_code == 402 and "нужно 3, осталось 2" in r.text
    assert env.calls == [] and latest(env) is None


def test_text_pdf_daily_cap():
    e = Env(daily_pages=2)
    e.plan["doc"] = {"docs": [PAGE]}
    c = e.login()
    batch_url(text_up(c, [("doc1.png", png()), ("doc2.png", png())]))
    r = text_up(c, [("doc3.png", png())])
    assert r.status_code == 503 and len(e.calls) == 2


def test_text_pdf_failed_pages(env):
    env.plan["doc"] = {"docs": [PAGE]}
    env.plan["down"] = {"fail": True, "cost": 0}
    url = batch_url(text_up(env.client, [("doc1.png", png()), ("down.png", png())]))
    b = latest(env)
    assert (b.status, b.pages_charged, b.docs_found, b.failed_files) == ("done", 1, 1, 1)
    page = env.client.get(url).text
    assert "Не распознано страниц: 1" in page and "boom" not in page
    texts = pdf_pages(env.client.get(url + "/download?fmt=pdf_text").content)
    assert len(texts) == 2 and SECRET_TEXT in texts[0] and "[страница 2 не распознана]" in texts[1]

    env.plan["bad"] = {"fail": True, "cost": 0.01}  # failed with a cost: free 3 times a day, then charged
    for _ in range(3):
        batch_url(text_up(env.client, [("bad.png", png())]))
        assert (latest(env).status, latest(env).pages_charged, latest(env).error_code) == ("failed", 0, "service")
    batch_url(text_up(env.client, [("bad.png", png())]))
    assert latest(env).pages_charged == 1


def test_text_pdf_backend_unavailable_is_free():
    def broken():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    e = Env(backend_factory=broken)
    c = e.login()
    batch_url(text_up(c, [("a.png", png())]))
    b = latest(e)
    assert (b.status, b.error_code, b.pages_charged) == ("failed", "service", 0) and balance_of(e) == 50


def test_text_pdf_encrypted_at_rest(env):
    env.plan["secret"] = {"docs": [PAGE]}
    url = batch_url(text_up(env.client, [("secret-contract-name.png", png())]))
    assert env.client.get(url + "/download?fmt=pdf_text").status_code == 200
    for needle in (SECRET_TEXT, "Договор поставки", "secret-contract-name"):
        for v in all_values(env.app):
            if isinstance(v, bytes):
                assert needle.encode("utf-8") not in v
            else:
                assert needle not in str(v)
    with env.db() as s:
        b = s.scalar(select(Batch))
        payload = crypto.decrypt(env.settings, b.public_id, b.results_enc)
        assert b.file_enc is None and b.file_expires_at is None  # a text PDF is laid out on download
    assert payload["kind"] == "text" and SECRET_TEXT in payload["pages"][0]["html"]
    assert "secret-contract-name" not in json.dumps(payload)


def test_malicious_html_cannot_make_links_or_read_files(env):
    evil = {"title": "x", "html": (
        '<p>Visible <a href="https://evil.example/steal">click here</a></p>'
        '<img src="file:///C:/Windows/win.ini"><img src="https://evil.example/pixel.png">'
        '<link rel="stylesheet" href="file:///etc/passwd"><style>body{background:url(file:///etc/passwd)}</style>'
        '<p style="background-image:url(https://evil.example/bg.png)">styled</p>'
        '<iframe src="https://evil.example"></iframe><object data="file:///etc/passwd"></object>'
        '<script>alert(1)</script><svg><image href="file:///etc/hosts"/></svg>'
    )}
    env.plan["evil"] = {"docs": [evil]}
    url = batch_url(text_up(env.client, [("evil.png", png())]))
    content = env.client.get(url + "/download?fmt=pdf_text").content
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        assert all(not p.get_links() and not p.get_images() for p in doc)
        assert doc.embfile_count() == 0
        body = "".join(p.get_text() for p in doc)
    assert "Visible" in body and "click here" in body and "styled" in body
    assert "alert(1)" not in body and "background" not in body
    for needle in (b"evil.example", b"passwd", b"win.ini", b"/URI", b"/JavaScript", b"/Launch"):
        assert needle not in content, needle


def test_text_pdf_ownership_and_csrf(env):
    env.plan["doc"] = {"docs": [PAGE]}
    assert text_up(env.client, [("doc.png", png())], token="0" * 64).status_code == 403
    assert env.calls == [] and latest(env) is None
    url = batch_url(text_up(env.client, [("doc.png", png())]))
    other = env.login(env.new_client(), "eve@example.com")
    for path in (url, url + "/status", url + "/download?fmt=pdf_text"):
        assert other.get(path).status_code == 404, path


def test_table_batch_has_no_pdf_download(env):
    url = batch_url(upload(env.client, [("a.png", png())]))
    for fmt in ("pdf_scan", "pdf_text"):
        assert env.client.get(f"{url}/download?fmt={fmt}").status_code == 404
    assert "Таблица" in env.client.get("/app/history").text


def test_logs_hold_no_document_data(caplog):
    caplog.set_level(logging.DEBUG)
    e = Env()
    c = e.login()
    e.plan["private"] = {"docs": [PAGE]}
    e.plan["boom"] = {"fail": True, "cost": 0.01}
    url = batch_url(text_up(c, [("private-text-name.png", png()), ("boom-file.png", png())]))
    c.get(url + "/download?fmt=pdf_text")
    messages = [r.getMessage() + " " + repr(r.args) for r in caplog.records]
    assert any("text pdf finished" in m for m in messages)
    for m in messages:
        for secret in (SECRET_TEXT, "Договор", "private-text-name", "boom-file", "boom secret",
                       MERCHANT):
            assert secret not in m, m


# ---------- data model ----------

def test_migration_0003_keeps_old_batches(tmp_path):
    from alembic import command

    from web.migrate import alembic_config

    db_url = f"sqlite:///{(tmp_path / 'old.sqlite3').as_posix()}"
    command.upgrade(alembic_config(db_url), "0002")
    from sqlalchemy import create_engine

    eng = create_engine(db_url)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email_fp, created_at, trial_pages_left, paid_pages_left) "
                          "VALUES (1, :fp, '2026-10-20 10:00:00', 0, 0)"), {"fp": "f" * 64})
        conn.execute(text(
            "INSERT INTO batches (public_id, user_id, doctype, fmt, one_document, lang, status, files_count, "
            "files_done, pages_estimated, pages_charged, work_units, docs_found, needs_review, failed_files, "
            "not_found, created_at, expires_at) VALUES ('old_batch_public_id_0001', 1, 'receipt', 'xlsx', 0, 'ru', "
            "'done', 1, 1, 1, 1, 1, 1, 0, 0, 0, '2026-10-20 10:00:00', '2027-01-18 10:00:00')"))
    eng.dispose()
    command.upgrade(alembic_config(db_url), "head")
    eng = create_engine(db_url)
    try:
        insp = inspect(eng)
        assert {"scan_usage", "scan_daily"} <= set(insp.get_table_names())
        cols = {c["name"]: c for c in insp.get_columns("batches")}
        assert "file_enc" in cols and "file_expires_at" in cols and cols["fmt"]["type"].length == 16
        with eng.connect() as conn:
            assert conn.execute(text("SELECT fmt, file_enc FROM batches")).all() == [("xlsx", None)]
    finally:
        eng.dispose()
    command.downgrade(alembic_config(db_url), "0002")
    command.upgrade(alembic_config(db_url), "head")
