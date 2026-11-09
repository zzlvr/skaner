"""Evidence: finding printed values on a page, flagging values that are not there, and the HTML report.

The pages are PDFs with a text layer made here with PyMuPDF, so no OCR is needed.
"""

from datetime import date

import pymupdf
import pytest

from docstruc import extract
from docstruc.core import files
from docstruc.core.models import ExtractionResult
from docstruc.evidence import NOT_FOUND_MESSAGE, locate_evidence
from docstruc.evidence import match
from docstruc.evidence.report import build_report
from docstruc.exporters.json_export import from_dict, to_dict

LINES = [
    (72, 60, "INVOICE   Invoice #: INV-2026-0419"),
    (72, 80, "Date: October 3, 2026      Due: 02.11.2026"),
    (72, 100, "Northwind Studio LLC"),
    (72, 120, "EIN 12-3456789"),
    (72, 160, "Website maintenance"),
    (330, 160, "1"),
    (380, 160, "1 200,00"),
    (460, 160, "1 200,00"),
    (72, 180, "Hosting"),
    (330, 180, "3"),
    (380, 180, "25.00"),
    (460, 180, "75.00"),
    (72, 220, "Subtotal"),
    (460, 220, "1 275.00"),
    (72, 240, "Tax (8%)"),
    (460, 240, "102.00"),
    (72, 260, "TOTAL DUE"),
    (460, 260, "1 377.00"),
]


def _pdf(lines=LINES) -> files.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return files.from_bytes(data, "inv.pdf")


DATA = {
    "invoice_number": "INV-2026-0419",
    "invoice_date": "2026-10-03",
    "due_date": "2026-11-02",
    "seller": {"name": "Northwind Studio LLC", "tax_id": "12-3456789"},
    "currency": "USD",
    "items": [
        {"description": "Website maintenance", "quantity": 1, "unit_price": 1200, "total": 1200},
        {"description": "Hosting", "quantity": 3, "unit_price": 25, "total": 75},
    ],
    "subtotal": 1275.0,
    "tax_rate": 8,
    "tax_amount": 102.0,
    "total": 1377.0,
}


def _result(data=None) -> ExtractionResult:
    return ExtractionResult(source="inv.pdf", doctype="invoice", model="fake", data=data or dict(DATA))


def test_numbers_read_the_way_they_are_printed():
    assert 1200.0 in match.numbers_in("1200,00")
    assert 586.21 in match.numbers_in("586,21")
    assert 1234.56 in match.numbers_in("1.234,56")
    assert 1234567.0 in match.numbers_in("1,234,567")
    assert match.numbers_in("$450.00") == {450.0}
    assert 16.0 in match.numbers_in("16%")
    assert match.numbers_in("14.10.2026") == set()  # a date is not a number
    assert match.numbers_in("INV-A") == set()


@pytest.mark.parametrize(
    "printed",
    ["14.10.2026", "14/10/26", "2026-10-14", "14-10-2026", "October 14, 2026", "14 Oct 2026", "14 октября 2026"],
)
def test_dates_match_in_any_common_format(printed):
    assert date(2026, 10, 14) in match._dates_in(printed)


def test_locates_values_next_to_where_they_are_printed():
    result = locate_evidence(_result(), [_pdf()])
    ev = result.evidence
    assert ev["invoice_number"].text.endswith("INV-2026-0419")
    assert ev["invoice_date"].text == "October 3, 2026"
    assert ev["due_date"].text == "02.11.2026"
    assert ev["seller.name"].text == "Northwind Studio LLC"
    assert ev["total"].text == "1 377.00"  # thousands written with a space are one number
    # the line amounts are taken from their own row, not from a row with the same number elsewhere
    assert ev["items.0.total"].box[1] < ev["items.1.total"].box[1] < ev["subtotal"].box[1]
    for e in ev.values():
        x0, y0, x1, y1 = e.box
        assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    assert not [i for i in result.issues if i.message == NOT_FOUND_MESSAGE]


def test_a_value_that_is_not_in_the_document_is_flagged():
    data = dict(DATA, total=1999.0, seller={"name": "Northwind Studio LLC", "tax_id": "99-0000000"})
    result = locate_evidence(_result(data), [_pdf()])
    flagged = {i.field for i in result.issues if i.message == NOT_FOUND_MESSAGE}
    assert flagged == {"total", "seller.tax_id"}
    assert all(i.severity == "warning" for i in result.issues)  # OCR can misread: a hint to look, not an error
    assert not result.needs_review
    assert "total" not in result.evidence


def test_a_line_amount_confirmed_by_quantity_times_price_is_not_flagged():
    lines = [line for line in LINES if line[2] != "75.00"] + [(460, 180, "7S.00")]  # a misread in the amount
    result = locate_evidence(_result(), [_pdf(lines)])
    assert "items.1.total" not in result.evidence
    assert not [i for i in result.issues if i.field == "items.1.total"]


def test_a_field_that_already_has_an_issue_is_not_flagged_twice():
    result = _result(dict(DATA, total=1999.0))
    from docstruc.core.models import Issue

    result.issues = [Issue(field="total", severity="error", message="Total does not match")]
    locate_evidence(result, [_pdf()])
    assert [i.message for i in result.issues if i.field == "total"] == ["Total does not match"]


def test_a_page_without_readable_text_gives_no_evidence_and_no_flags():
    blank = pymupdf.open()
    blank.new_page()
    document = files.from_bytes(blank.tobytes(), "scan.pdf")
    result = locate_evidence(_result(), [document])
    assert result.evidence == {} and result.issues == []


def test_evidence_survives_a_json_round_trip():
    result = locate_evidence(_result(), [_pdf()])
    back = from_dict(to_dict([result]))[0]
    assert back.evidence["total"].box == result.evidence["total"].box
    assert "evidence" not in to_dict([_result()])[0]  # not located: the key is absent


class FakeBackend:
    name = "fake"

    def extract(self, parts, schema, instructions):
        return schema.model_validate(DATA)


def test_pipeline_locates_only_when_asked():
    document = _pdf()
    assert extract(document, "invoice", backend=FakeBackend()).evidence == {}
    located = extract(document, "invoice", backend=FakeBackend(), locate=True)
    assert located.evidence["total"].text == "1 377.00"


def test_report_escapes_everything_that_comes_from_a_document():
    data = dict(DATA, seller={"name": "<script>alert(1)</script>", "tax_id": '"><img src=x onerror=alert(1)>'})
    result = locate_evidence(_result(data), [_pdf()])
    html = build_report([(result, [_pdf()])])
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert html.count("<script>") == 1  # only the report's own script


def test_report_shows_status_marks_and_the_checks():
    result = locate_evidence(_result(dict(DATA, total=1999.0)), [_pdf()])
    html = build_report([(result, [_pdf()])], title="Batch 1")
    assert "<title>Batch 1</title>" in html
    assert 'class="box warning' not in html or "Not found in the document text" in html
    assert "Checks passed" in html or "Needs review" in html
    assert "data:image/jpeg;base64," in html
    assert "Not found in the document text" in html
