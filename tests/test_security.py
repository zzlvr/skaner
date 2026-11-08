"""Things a user or a document must not be able to do."""

import time
from io import BytesIO

import pytest
from openpyxl import load_workbook
from PIL import Image

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
