"""Things a user or a document must not be able to do."""

import time
from io import BytesIO

import pytest
from openpyxl import load_workbook
from PIL import Image

from bot.quota import QuotaStore
from bot.session import Batch, Sessions
from docstruc.core import files
from docstruc.core.models import Document, ExtractionResult
from docstruc.exporters import export
from docstruc.exporters.excel import build_workbook
from docstruc.exporters.onec import build_workbook as onec_workbook

EVIL = ExtractionResult(
    source="=1+1.jpg", doctype="receipt", model="x",
    data={"merchant_name": '=HYPERLINK("http://evil.example","Click")', "total": 5,
          "items": [{"description": "@SUM(1,1)", "quantity": 1, "unit_price": 5, "total": 5}]},
)


def _formula_cells(wb):
    return [c.coordinate for ws in wb for row in ws.iter_rows() for c in row if c.data_type == "f"]


def test_document_text_never_becomes_an_excel_formula(tmp_path):
    assert _formula_cells(build_workbook([EVIL], lang="ru")) == []
    assert _formula_cells(onec_workbook([EVIL])) == []
    out = tmp_path / "x.xlsx"
    export([EVIL], out, fmt="xlsx")
    wb = load_workbook(out)
    assert _formula_cells(wb) == []
    assert '=HYPERLINK("http://evil.example","Click")' in [c.value for ws in wb for row in ws.iter_rows() for c in row]


def test_csv_values_that_look_like_formulas_are_escaped(tmp_path):
    out = tmp_path / "x.csv"
    export([EVIL], out, fmt="csv")
    text = out.read_text(encoding="utf-8-sig") + (tmp_path / "x_items.csv").read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text and "'@SUM" in text and "'=1+1.jpg" in text


def _image(fmt="JPEG", size=(40, 60)):
    out = BytesIO()
    Image.new("RGB", size, "white").save(out, fmt)
    return out.getvalue()


def test_uploaded_images_are_checked_by_content():
    assert files.from_bytes(_image("PNG"), "photo.jpg").mime_type == "image/png"  # the name lied
    with pytest.raises(files.UnsupportedFileError):
        files.from_bytes(b"not an image at all", "photo.jpg")
    with pytest.raises(files.UnsupportedFileError):
        files.from_bytes(b"<html>", "doc.pdf")


def test_decompression_bomb_is_refused():
    huge = BytesIO()
    Image.new("1", (8000, 8000)).save(huge, "PNG")  # 64 MP, tiny file
    assert len(huge.getvalue()) < 100_000
    with pytest.raises(files.UnsupportedFileError, match="too large"):
        files.from_bytes(huge.getvalue(), "bomb.png")


def test_oversized_images_are_shrunk_for_the_api():
    from docstruc.llm.claude import _fit_image
    big = Document(name="big.png", mime_type="image/png", data=_image("PNG", (8200, 100)))
    data, mime = _fit_image(big)
    assert mime == "image/jpeg" and max(Image.open(BytesIO(data)).size) <= 4000
    small = Document(name="s.jpg", mime_type="image/jpeg", data=_image())
    assert _fit_image(small) == (small.data, "image/jpeg")


def test_idle_sessions_are_forgotten(monkeypatch):
    import bot.session as session
    sessions = Sessions()
    batch = sessions.get(1)
    batch.files.append(Document(name="a.jpg", mime_type="image/jpeg", data=b"x" * 1000))
    assert sessions.total_size() == 1000
    batch.touched = time.monotonic() - session.IDLE_SECONDS - 1
    sessions.get(2)
    assert sessions.total_size() == 0


def test_free_scan_pages_are_counted_per_day():
    from datetime import date
    store = QuotaStore(":memory:", free_docs=5, daily_limit=50)
    store.add_scan_pages(1, 30, today=date(2026, 10, 19))
    store.add_free_failures(1, 1, today=date(2026, 10, 19))
    assert store.scan_pages_used(1, today=date(2026, 10, 19)) == 30
    assert store.free_failures_used(1, today=date(2026, 10, 19)) == 1


def test_forged_feedback_buttons_are_ignored():
    import asyncio
    from types import SimpleNamespace
    from bot.main import BotApp

    app = BotApp.__new__(BotApp)
    app.quota = QuotaStore(":memory:", free_docs=5, daily_limit=50)
    edits = []

    async def edit_reply_markup(reply_markup=None):
        edits.append(1)

    update = SimpleNamespace(callback_query=SimpleNamespace(edit_message_reply_markup=edit_reply_markup),
                             effective_user=SimpleNamespace(language_code="ru"), effective_chat=SimpleNamespace(id=1))
    asyncio.run(app._on_feedback_button(update, None, Batch(), "bad:" + "x" * 50))
    assert edits == [] and app.quota.stats()["fb_bad"] == 0


def test_database_backup_keeps_recent_copies(tmp_path):
    import sqlite3
    import importlib.util
    spec = importlib.util.spec_from_file_location("backup_db", "deploy/backup_db.py")
    backup_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup_db)
    db = tmp_path / "bot.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE users (user_id INTEGER, credits INTEGER)")
        con.execute("INSERT INTO users VALUES (1, 30)")
    folder = tmp_path / "backups"
    folder.mkdir()
    for n in range(20):
        (folder / f"bot-2025-01-{n + 1:02d}.sqlite3").write_bytes(b"old")
    copy = backup_db.backup(db)
    with sqlite3.connect(copy) as con:
        assert con.execute("SELECT credits FROM users").fetchone() == (30,)
    assert len(list(folder.glob("bot-*.sqlite3"))) == backup_db.KEEP
