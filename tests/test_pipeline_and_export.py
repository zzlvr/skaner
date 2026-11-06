import json

import pytest
from openpyxl import load_workbook

from docstruc import extract
from docstruc.core import files
from docstruc.core.models import ExtractionResult
from docstruc.doctypes.common import LineItem
from docstruc.exporters import export


class FakeBackend:
    """Stands in for a real model so the pipeline can be tested offline."""

    name = "fake"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def extract(self, parts, schema, instructions):
        self.calls.append(([p.name for p in parts], schema.__name__))
        return schema.model_validate(self.result)


RECEIPT_DATA = {
    "merchant_name": "Кофейня «Жибек»",
    "date": "2026-10-01",
    "currency": "KZT",
    "items": [
        {"description": "Капучино", "quantity": 2, "unit_price": 1200, "total": 2400},
        {"description": "Круассан", "quantity": 1, "unit_price": 900, "total": 950},
    ],
    "total": 3350,
}


def _jpeg() -> bytes:
    from io import BytesIO
    from PIL import Image
    out = BytesIO()
    Image.new("RGB", (40, 60), "white").save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def receipt_file(tmp_path):
    path = tmp_path / "r1.jpg"
    path.write_bytes(_jpeg())
    return path


def test_extract_runs_backend_and_validation(receipt_file):
    backend = FakeBackend(RECEIPT_DATA)
    result = extract(receipt_file, "receipt", backend=backend)

    assert backend.calls == [(["r1.jpg"], "Receipt")]
    assert result.data["merchant_name"] == "Кофейня «Жибек»"
    assert result.needs_review
    assert result.flagged_fields == {"items.1.total"}


def test_extract_multipart_document(tmp_path):
    part_paths = []
    for name in ("long_1.jpg", "long_2.jpg"):
        p = tmp_path / name
        p.write_bytes(_jpeg())
        part_paths.append(p)
    backend = FakeBackend(RECEIPT_DATA)
    result = extract(part_paths, "receipt", backend=backend)
    assert backend.calls == [(["long_1.jpg", "long_2.jpg"], "Receipt")]
    assert result.source == "long_1.jpg + long_2.jpg"


def test_unknown_doctype(receipt_file):
    with pytest.raises(ValueError, match="Unknown document type"):
        extract(receipt_file, "passport", backend=FakeBackend({}))


def test_unsupported_and_empty_files():
    with pytest.raises(files.UnsupportedFileError):
        files.from_bytes(b"x", "notes.txt")
    with pytest.raises(files.UnsupportedFileError):
        files.from_bytes(b"", "a.pdf")


@pytest.fixture
def results(receipt_file):
    ok = extract(receipt_file, "receipt", backend=FakeBackend({**RECEIPT_DATA, "items": RECEIPT_DATA["items"][:1], "total": 2400}))
    bad = extract(receipt_file, "receipt", backend=FakeBackend(RECEIPT_DATA))
    failed = ExtractionResult.failed("broken.pdf", "receipt", "timeout")
    return [ok, bad, failed]


def test_excel_export(results, tmp_path):
    out = tmp_path / "out.xlsx"
    export(results, out)
    wb = load_workbook(out)
    assert wb.sheetnames == ["Documents", "Line items", "Review"]

    docs = list(wb["Documents"].values)
    assert docs[0][:2] == ("file", "status")
    assert [row[1] for row in docs[1:]] == ["ok", "needs review", "needs review"]

    items = wb["Line items"]
    assert items.max_row == 1 + 1 + 2
    flagged_row = items[4]  # second item of the bad receipt
    assert flagged_row[0].fill.fgColor.rgb.endswith("FFF2CC")

    review = list(wb["Review"].values)
    assert {(r[0], r[1]) for r in review[1:]} == {("r1.jpg", "items.1.total"), ("broken.pdf", "document")}


def test_json_and_csv_export(results, tmp_path):
    export(results, tmp_path / "out.json")
    data = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert [d["needs_review"] for d in data] == [False, True, True]
    assert data[1]["data"]["items"][0]["description"] == "Капучино"

    written = export(results, tmp_path / "out.csv")
    assert [p.name for p in written] == ["out.csv", "out_items.csv"]
    assert "Капучино" in (tmp_path / "out_items.csv").read_text(encoding="utf-8-sig")


def test_unsupported_output_format(results, tmp_path):
    with pytest.raises(ValueError, match="Unsupported output format"):
        export(results, tmp_path / "out.txt")
