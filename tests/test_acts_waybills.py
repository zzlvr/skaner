from datetime import datetime

import pytest
from openpyxl import load_workbook

from docstruc.core.models import ExtractionResult
from docstruc.doctypes import DOCTYPES
from docstruc.exporters import export
from docstruc.llm.claude import strict_json_schema

ACT_DATA = {
    "act_number": "12",
    "act_date": "2026-10-01",
    "contract_number": "Д-45",
    "contract_date": "2026-01-15",
    "period_from": "2026-09-01",
    "period_to": "2026-09-30",
    "contractor": {"name": "ТОО «Сервис»", "tax_id": "940140000385", "address": "Астана"},
    "customer": {"name": "ТОО «Клиент»", "tax_id": "971240001315", "address": None},
    "currency": "KZT",
    "items": [
        {"description": "Обслуживание ПК", "unit": "час", "quantity": 10, "unit_price": 5000, "total": 50000},
        {"description": "Настройка сети", "unit": "усл.", "quantity": 1, "unit_price": 30000, "total": 30000},
    ],
    "subtotal": 80000,
    "tax_rate": 16,
    "tax_amount": 12800,
    "total": 92800,
}

WAYBILL_DATA = {
    "waybill_number": "305",
    "waybill_date": "2026-10-05",
    "sender": {"name": "ТОО «Склад»", "tax_id": "940140000385"},
    "recipient": {"name": "ИП Ахметов", "tax_id": "971240001315"},
    "currency": "KZT",
    "items": [{"description": "Бумага А4", "unit": "пачка", "quantity": 20, "unit_price": 2500, "total": 50000}],
    "tax_amount": 6896.55,  # VAT already included in prices, as on form З-2
    "total": 50000,
}


def check(name, data):
    doctype = DOCTYPES[name]
    return doctype.validate(doctype.schema.model_validate(data))


def test_new_types_are_registered():
    assert {"act", "waybill"} <= set(DOCTYPES)


@pytest.mark.parametrize("name,data", [("act", ACT_DATA), ("waybill", WAYBILL_DATA)])
def test_correct_documents_pass(name, data):
    assert [i for i in check(name, data) if i.severity == "error"] == []


def test_act_with_wrong_total_and_bin_is_flagged():
    bad = {**ACT_DATA, "total": 93800, "customer": {"name": "X", "tax_id": "971240001316"}}
    assert {i.field for i in check("act", bad) if i.severity == "error"} == {"total", "customer.tax_id"}


@pytest.mark.parametrize("name", ["act", "waybill"])
def test_schemas_work_with_claude(name):
    schema = strict_json_schema(DOCTYPES[name].schema)
    assert schema["properties"]["items"]["items"]["properties"]["unit"]["type"] == ["string", "null"]


def test_1c_export_maps_parties_contract_and_units(tmp_path):
    out = tmp_path / "1c.xlsx"
    export([
        ExtractionResult(source="act.jpg", doctype="act", model="fake", data=ACT_DATA),
        ExtractionResult(source="wb.jpg", doctype="waybill", model="fake", data=WAYBILL_DATA),
    ], out, fmt="1c")
    wb = load_workbook(out)

    docs = [dict(zip([c.value for c in wb["Документы"][1]], [c.value for c in row])) for row in wb["Документы"].iter_rows(min_row=2)]
    act, waybill = docs
    assert (act["Вид документа"], act["Поставщик"], act["БИН/ИИН покупателя"]) == ("Акт выполненных работ", "ТОО «Сервис»", "971240001315")
    assert (act["Договор"], act["Дата договора"]) == ("Д-45", datetime(2026, 1, 15))
    assert (waybill["Вид документа"], waybill["Покупатель"], waybill["Итого"]) == ("Накладная", "ИП Ахметов", 50000)

    items = wb["Товары и услуги"]
    header = [c.value for c in items[1]]
    assert [row[header.index("Ед. изм.")] for row in items.iter_rows(min_row=2, values_only=True)] == ["час", "усл.", "пачка"]
    assert len(list(wb["Контрагенты"].iter_rows(min_row=2))) == 2  # same BINs on both documents


def test_russian_excel_has_russian_columns(tmp_path):
    from docstruc.exporters.excel import build_workbook
    wb = build_workbook([ExtractionResult(source="act.jpg", doctype="act", model="fake", data=ACT_DATA)], lang="ru")
    columns = [c.value for c in wb["Документы"][1]]
    assert {"Номер акта", "Исполнитель", "БИН/ИИН заказчика", "Номер договора", "Период с"} <= set(columns)
    assert "Ед. изм." in [c.value for c in wb["Позиции"][1]]
