from datetime import date, datetime

import pytest
from openpyxl import load_workbook

from docstruc.core.models import ExtractionResult, Issue
from docstruc.exporters import export
from docstruc.exporters.onec import DOC_COLUMNS, ITEM_COLUMNS, remark_ru


def receipt(source, tax_id="261010000123", issues=()):
    return ExtractionResult(
        source=source,
        doctype="receipt",
        model="fake",
        data={
            "merchant_name": "ТОО «Кофейня Жибек»",
            "merchant_tax_id": tax_id,
            "merchant_address": "г. Алматы, пр. Абая, 10",
            "receipt_number": "004512",
            "date": "2026-10-14",
            "currency": "KZT",
            "items": [
                {"description": "Капучино", "quantity": 2, "unit_price": 1200, "total": 2400},
                {"description": "Круассан", "quantity": 1, "unit_price": 950, "total": 950},
            ],
            "subtotal": None,
            "discount": None,
            "tax_amount": 359.14,
            "total": 3350,
        },
        issues=list(issues),
    )


INVOICE = ExtractionResult(
    source="inv.pdf",
    doctype="invoice",
    model="fake",
    data={
        "invoice_number": "17",
        "invoice_date": "2026-09-30",
        "due_date": "2026-10-30",
        "seller": {"name": "ТОО «Поставщик»", "tax_id": "940140000385", "address": "Астана"},
        "buyer": {"name": "ТОО «Покупатель»", "tax_id": "971240001315", "address": None},
        "currency": "KZT",
        "items": [{"description": "Услуги", "quantity": 1, "unit_price": 100000, "total": 100000}],
        "subtotal": 100000,
        "tax_rate": 16,
        "tax_amount": 16000,
        "total": 116000,
    },
)


@pytest.fixture
def workbook(tmp_path):
    flagged = receipt("r2.jpg", issues=[Issue(field="items.1.total", severity="error", message="mismatch")])
    out = tmp_path / "for_1c.xlsx"
    export([receipt("r1.jpg"), flagged, INVOICE], out, fmt="1c")
    return load_workbook(out)


def test_sheets_and_columns(workbook):
    assert workbook.sheetnames == ["Товары и услуги", "Документы", "Контрагенты", "Инструкция"]
    assert [c.value for c in workbook["Товары и услуги"][1]] == ITEM_COLUMNS
    assert [c.value for c in workbook["Документы"][1]] == DOC_COLUMNS


def test_one_row_per_line_item_with_header_repeated(workbook):
    rows = list(workbook["Товары и услуги"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 2 + 2 + 1
    first = dict(zip(ITEM_COLUMNS, rows[0]))
    assert first["Номер документа"] == "004512"
    assert first["БИН/ИИН поставщика"] == "261010000123"
    assert first["Номенклатура"] == "Капучино"
    assert first["Сумма"] == 2400


def test_dates_are_real_dates_in_1c_format(workbook):
    ws = workbook["Документы"]
    col = DOC_COLUMNS.index("Дата документа") + 1
    cell = ws.cell(row=2, column=col)
    assert isinstance(cell.value, (date, datetime)) and cell.value.date() == date(2026, 10, 14)
    assert cell.number_format == "DD.MM.YYYY"


def test_invoice_header_and_vat(workbook):
    row = dict(zip(DOC_COLUMNS, next(workbook["Документы"].iter_rows(min_row=4, values_only=True))))
    assert row["Вид документа"] == "Счёт (инвойс)"
    assert row["Покупатель"] == "ТОО «Покупатель»"
    assert (row["Ставка НДС, %"], row["Сумма НДС"], row["Итого"]) == (16, 16000, 116000)
    assert row["Статус"] == "OK"


def test_flagged_document_and_line_are_highlighted(workbook):
    docs = workbook["Документы"]
    status_col = DOC_COLUMNS.index("Статус") + 1
    assert docs.cell(row=3, column=status_col).value == "Требует проверки"
    assert docs.cell(row=3, column=status_col).fill.fgColor.rgb.endswith("FFF2CC")
    items = workbook["Товары и услуги"]
    assert items.cell(row=5, column=1).fill.fgColor.rgb.endswith("FFF2CC")  # second line of the flagged receipt
    assert not items.cell(row=4, column=1).fill.fgColor.rgb.endswith("FFF2CC")


def test_counterparties_are_deduplicated_by_bin(workbook):
    rows = list(workbook["Контрагенты"].iter_rows(min_row=2, values_only=True))
    assert [(r[1], r[3]) for r in rows] == [
        ("261010000123", "Поставщик"),
        ("940140000385", "Поставщик"),
        ("971240001315", "Покупатель"),
    ]


def test_format_and_extension_must_agree(tmp_path):
    with pytest.raises(ValueError, match="writes a .xlsx file"):
        export([INVOICE], tmp_path / "x.csv", fmt="1c")
    with pytest.raises(ValueError, match="Unknown format"):
        export([INVOICE], tmp_path / "x.xlsx", fmt="xml")


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        (Issue(field="total", severity="error", message="Total 991.00 does not match subtotal 825.00, tax 66.00"),
         "Итого: итог 991.00 не сходится: подытог 825.00, налог 66.00"),
        (Issue(field="items.1.total", severity="error", message="quantity × unit price = 1352.00, but line total is 1362.00"),
         "Строка 2, сумма: количество × цена = 1352.00, а сумма строки 1362.00"),
        (Issue(field="merchant_tax_id", severity="error", message="12-digit number fails the IIN/BIN checksum"),
         "БИН/ИИН поставщика: ИИН/БИН не проходит проверку контрольной цифры"),
        (Issue(field="date", severity="error", message="Hard to read on the document, please check"),
         "Дата документа: плохо читается в документе, сверьте с оригиналом"),
    ],
)
def test_remarks_are_in_russian(issue, expected):
    assert remark_ru(issue) == expected


def test_only_review_issues_go_to_remarks(tmp_path):
    r = receipt("r.jpg", issues=[
        Issue(field="items.0.description", severity="warning", message="Hard to read on the document, please check"),
        Issue(field="date", severity="error", message="Hard to read on the document, please check"),
    ])
    out = tmp_path / "x.xlsx"
    export([r], out, fmt="1c")
    row = dict(zip(DOC_COLUMNS, next(load_workbook(out)["Документы"].iter_rows(min_row=2, values_only=True))))
    assert row["Замечания"] == "Дата документа: плохо читается в документе, сверьте с оригиналом"


def test_quantity_has_no_trailing_decimal_sign(tmp_path):
    """QA 2026-10-25: the format "0.###" showed a whole quantity 12 as "12," in Excel (and in what 1C reads)."""
    weighed = receipt("w.jpg")
    weighed.data["items"].append({"description": "Сыр", "quantity": 1.245, "unit_price": 1000, "total": 1245})
    path = tmp_path / "out.xlsx"
    export([weighed], path, fmt="1c")
    ws = load_workbook(path)["Товары и услуги"]
    col = ITEM_COLUMNS.index("Количество") + 1
    formats = {ws.cell(row=r, column=col).value: ws.cell(row=r, column=col).number_format for r in range(2, ws.max_row + 1)}
    assert formats == {2: "0", 1: "0", 1.245: "0.###"}
