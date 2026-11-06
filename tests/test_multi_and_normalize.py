from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook

from docstruc import extract_all
from docstruc.core.models import Document, ExtractionResult, Issue
from docstruc.core.normalize import clean_item_name, clean_number
from docstruc.exporters import excel

RECEIPT_A = {"merchant_name": "ТОО «SALEKZ»", "receipt_number": "№00003344", "date": "2026-10-15", "total": 3520,
             "items": [{"description": "^^#КУС ВКУС ГОЛЕНЬ", "quantity": 0.814, "unit_price": 2729, "total": 2221},
                       {"description": "АЙС КУРТ", "quantity": 1, "unit_price": 1299, "total": 1299}]}
RECEIPT_B = {"merchant_name": "ИП DURSUN", "date": "2026-10-15", "total": 536,
             "items": [{"description": "#МОРКОВЬ, КГ", "quantity": 0.534, "unit_price": 161, "total": 86},
                       {"description": "БУЛОЧКА", "quantity": 1, "unit_price": 450, "total": 450}]}


class PageBackend:
    name = "fake"

    def __init__(self, documents):
        self.documents = documents
        self.instructions = None

    def extract(self, parts, schema, instructions):
        self.instructions = instructions
        return schema.model_validate({"documents": self.documents})


PHOTO = Document(name="photo.jpg", mime_type="image/jpeg", data=b"x")


def test_two_receipts_on_one_photo_become_two_results():
    backend = PageBackend([RECEIPT_A, RECEIPT_B])
    results = extract_all(PHOTO, "receipt", backend=backend)
    assert [r.source for r in results] == ["photo.jpg #1", "photo.jpg #2"]
    assert [r.data["total"] for r in results] == [3520, 536]
    assert all(not r.needs_review for r in results)
    assert "several separate documents" in backend.instructions


def test_single_document_keeps_the_file_name():
    results = extract_all(PHOTO, "receipt", backend=PageBackend([RECEIPT_B]))
    assert [r.source for r in results] == ["photo.jpg"]


def test_no_document_found_needs_review():
    [result] = extract_all(PHOTO, "receipt", backend=PageBackend([]))
    assert result.needs_review and result.issues[0].message == "No document found in the file"


def test_extracted_data_is_cleaned():
    first, second = extract_all(PHOTO, "receipt", backend=PageBackend([RECEIPT_A, RECEIPT_B]))
    assert first.data["receipt_number"] == "00003344"
    assert first.data["items"][0]["description"] == "КУС ВКУС ГОЛЕНЬ"
    assert second.data["items"][0]["description"] == "МОРКОВЬ, КГ"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [("^^#КУС ВКУС", "КУС ВКУС"), ("* МОЛОКО 2,5%", "МОЛОКО 2,5%"), ("Coffee #2", "Coffee #2"), ("###", "###")],
)
def test_clean_item_name(raw, clean):
    assert clean_item_name(raw) == clean


@pytest.mark.parametrize(("raw", "clean"), [("№00003344", "00003344"), ("No. 17", "17"), ("# A-1", "A-1"), ("INV-5", "INV-5")])
def test_clean_number(raw, clean):
    assert clean_number(raw) == clean


def test_russian_excel_labels_dates_and_messages():
    result = ExtractionResult(
        source="r.jpg", doctype="receipt", model="fake",
        data={"merchant_name": "Shop", "date": "2026-10-15", "total": 5, "items": []},
        issues=[Issue(field="date", severity="error", message="Hard to read on the document, please check")],
    )
    wb = load_workbook(BytesIO(excel.to_bytes([result], lang="ru")))
    docs = wb["Документы"]
    header = [c.value for c in docs[1]]
    assert header[:3] == ["Файл", "Статус", "Продавец"]
    row = dict(zip(header, [c.value for c in docs[2]]))
    assert row["Статус"] == "Требует проверки"
    assert isinstance(row["Дата"], datetime)
    review = list(wb["Проверка"].iter_rows(min_row=2, values_only=True))
    assert review == [("r.jpg", "Дата", "проверить", "плохо читается в документе, сверьте с оригиналом")]
