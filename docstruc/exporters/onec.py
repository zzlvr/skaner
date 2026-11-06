"""Excel prepared for import into 1C (e.g. 1C:Бухгалтерия для Казахстана).

1C loads such files with the standard processing "Загрузка данных из табличного документа":
the accountant maps columns to document fields. So the file uses Russian column names,
real dates (ДД.ММ.ГГГГ), plain numbers, one row per line item with the document header
repeated (the shape that processing expects for a tabular part), and a counterparty sheet
keyed by BIN/IIN, which is how 1C matches counterparties.

Sheets: "Товары и услуги", "Документы", "Контрагенты", "Инструкция".
"""

from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from docstruc.core.models import ExtractionResult, Issue
from docstruc.exporters import i18n
from docstruc.exporters.excel import FLAG_FILL, HEADER_FONT
from docstruc.exporters.safe import neutralize_workbook

DATE_FORMAT = "DD.MM.YYYY"
MONEY_FORMAT = "0.00"
QTY_FORMAT = "0.###"

KIND = {
    "invoice": "Счёт (инвойс)", "receipt": "Чек",
    "act": "Акт выполненных работ", "waybill": "Накладная",
}

# Where each document type keeps its number, date, supplier and customer.
# Receipts have no buyer and keep the merchant in flat fields, see _header.
PARTIES = {
    "invoice": ("invoice_number", "invoice_date", "seller", "buyer"),
    "act": ("act_number", "act_date", "contractor", "customer"),
    "waybill": ("waybill_number", "waybill_date", "sender", "recipient"),
}

DOC_COLUMNS = [
    "№", "Файл", "Вид документа", "Номер документа", "Дата документа", "Срок оплаты",
    "Поставщик", "БИН/ИИН поставщика", "Адрес поставщика", "Покупатель", "БИН/ИИН покупателя",
    "Договор", "Дата договора", "Валюта", "Сумма без скидки", "Скидка", "Ставка НДС, %", "Сумма НДС", "Итого", "Статус", "Замечания",
]
ITEM_COLUMNS = [
    "№ документа", "Номер документа", "Дата документа", "Поставщик", "БИН/ИИН поставщика",
    "Номенклатура", "Ед. изм.", "Количество", "Цена", "Сумма", "Валюта", "Статус",
]
PARTY_COLUMNS = ["Наименование", "БИН/ИИН", "Адрес", "Роль"]

# Engine field → column on the "Документы" sheet, for highlighting flagged values.
FIELD_COLUMNS = {
    "invoice_number": "Номер документа", "receipt_number": "Номер документа",
    "invoice_date": "Дата документа", "date": "Дата документа", "due_date": "Срок оплаты",
    "seller.name": "Поставщик", "merchant_name": "Поставщик",
    "seller.tax_id": "БИН/ИИН поставщика", "merchant_tax_id": "БИН/ИИН поставщика",
    "seller.address": "Адрес поставщика", "merchant_address": "Адрес поставщика",
    "buyer.name": "Покупатель", "buyer.tax_id": "БИН/ИИН покупателя",
    "act_number": "Номер документа", "waybill_number": "Номер документа",
    "act_date": "Дата документа", "waybill_date": "Дата документа",
    "contractor.name": "Поставщик", "sender.name": "Поставщик",
    "contractor.tax_id": "БИН/ИИН поставщика", "sender.tax_id": "БИН/ИИН поставщика",
    "contractor.address": "Адрес поставщика", "sender.address": "Адрес поставщика",
    "customer.name": "Покупатель", "recipient.name": "Покупатель",
    "customer.tax_id": "БИН/ИИН покупателя", "recipient.tax_id": "БИН/ИИН покупателя",
    "contract_number": "Договор", "contract_date": "Дата договора",
    "currency": "Валюта", "subtotal": "Сумма без скидки", "discount": "Скидка",
    "tax_rate": "Ставка НДС, %", "tax_amount": "Сумма НДС", "total": "Итого",
}
NUMBER_FORMATS = {
    "Дата документа": DATE_FORMAT, "Срок оплаты": DATE_FORMAT, "Дата договора": DATE_FORMAT,
    "Сумма без скидки": MONEY_FORMAT, "Скидка": MONEY_FORMAT, "Сумма НДС": MONEY_FORMAT, "Итого": MONEY_FORMAT,
    "Количество": QTY_FORMAT, "Цена": MONEY_FORMAT, "Сумма": MONEY_FORMAT,
}

INSTRUCTIONS = [
    "Как загрузить данные в 1С",
    "",
    "1. Сначала проверьте строки со статусом «Требует проверки»: жёлтые ячейки сверьте с оригиналом, причина — в колонке «Замечания».",
    "2. Контрагенты: лист «Контрагенты». 1С сопоставляет контрагентов по БИН/ИИН; новых можно загрузить в справочник «Контрагенты» той же обработкой.",
    "3. Откройте обработку «Загрузка данных из табличного документа» (обычно: Все функции → Обработки; в некоторых конфигурациях её нужно открыть как внешнюю обработку).",
    "4. Режим «Загрузка в табличную часть»: выберите документ (например, «Поступление товаров и услуг») и табличную часть «Товары» или «Услуги». Накладные обычно идут в «Товары», акты выполненных работ — в «Услуги».",
    "5. Скопируйте строки одного документа с листа «Товары и услуги» и сопоставьте колонки: Номенклатура → Номенклатура, Ед. изм., Количество, Цена, Сумма.",
    "6. Шапку документа (номер, дату, контрагента) заполните по листу «Документы».",
    "",
    "Даты — в формате ДД.ММ.ГГГГ, суммы — числа с двумя знаками. НДС указан по документу в целом (лист «Документы»), как напечатан в оригинале.",
    "Названия меню зависят от конфигурации и версии 1С. Если у вас другая схема загрузки — пришлите пример, файл подстроим под неё.",
]


def remark_ru(issue: Issue) -> str:
    """A validation issue as a short Russian remark for accountants."""
    return f"{i18n.field(issue.field, 'ru', FIELD_COLUMNS)}: {i18n.message(issue.message, 'ru')}"


def _to_date(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


def _party(data: dict, key: str) -> dict:
    return data.get(key) or {}


def _header(r: ExtractionResult, n: int) -> dict[str, Any]:
    d = r.data
    if r.doctype in PARTIES:
        number_key, date_key, seller_key, buyer_key = PARTIES[r.doctype]
        seller, buyer = _party(d, seller_key), _party(d, buyer_key)
        number, doc_date = d.get(number_key), d.get(date_key)
        supplier = (seller.get("name"), seller.get("tax_id"), seller.get("address"))
        customer = (buyer.get("name"), buyer.get("tax_id"))
    else:
        number, doc_date = d.get("receipt_number"), d.get("date")
        supplier = (d.get("merchant_name"), d.get("merchant_tax_id"), d.get("merchant_address"))
        customer = (None, None)
    # Only what needs a human check; confirmed doubts and cosmetic warnings would just clutter the file.
    remarks = "; ".join(remark_ru(i) for i in r.issues if i.severity == "error")
    return {
        "№": n,
        "Файл": r.source,
        "Вид документа": KIND.get(r.doctype, r.doctype),
        "Номер документа": number,
        "Дата документа": _to_date(doc_date),
        "Срок оплаты": _to_date(d.get("due_date")),
        "Поставщик": supplier[0],
        "БИН/ИИН поставщика": supplier[1],
        "Адрес поставщика": supplier[2],
        "Покупатель": customer[0],
        "БИН/ИИН покупателя": customer[1],
        "Договор": d.get("contract_number"),
        "Дата договора": _to_date(d.get("contract_date")),
        "Валюта": d.get("currency"),
        "Сумма без скидки": d.get("subtotal"),
        "Скидка": d.get("discount"),
        "Ставка НДС, %": d.get("tax_rate"),
        "Сумма НДС": d.get("tax_amount"),
        "Итого": d.get("total"),
        "Статус": "Требует проверки" if r.needs_review else "OK",
        "Замечания": remarks or None,
    }


def _write_sheet(ws, columns: list[str], rows: list[dict]) -> None:
    ws.append(columns)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in rows:
        ws.append([row.get(c) for c in columns])
    for idx, col in enumerate(columns, start=1):
        fmt = NUMBER_FORMATS.get(col)
        if fmt:
            for (cell,) in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
                cell.number_format = fmt
                # "0.###" shows a whole number as "12," (a trailing decimal sign) in Excel and in what 1C reads.
                if fmt == QTY_FORMAT and isinstance(cell.value, (int, float)) and float(cell.value).is_integer():
                    cell.number_format = "0"
        width = max([len(col)] + [len(str(r.get(col) or "")) for r in rows[:200]])
        ws.column_dimensions[get_column_letter(idx)].width = min(max(width + 2, 10), 50)
    ws.freeze_panes = "A2"


def build_workbook(results: list[ExtractionResult]) -> Workbook:
    headers = [_header(r, n) for n, r in enumerate(results, start=1)]

    items, flagged_item_rows = [], set()
    for r, h in zip(results, headers):
        flagged_lines = {int(i.field.split(".")[1]) for i in r.issues if i.field.startswith("items.") and i.severity == "error"}
        for line, item in enumerate(r.data.get("items") or []):
            items.append({
                "№ документа": h["№"],
                "Номер документа": h["Номер документа"],
                "Дата документа": h["Дата документа"],
                "Поставщик": h["Поставщик"],
                "БИН/ИИН поставщика": h["БИН/ИИН поставщика"],
                "Номенклатура": item.get("description"),
                "Ед. изм.": item.get("unit"),
                "Количество": item.get("quantity"),
                "Цена": item.get("unit_price"),
                "Сумма": item.get("total"),
                "Валюта": h["Валюта"],
                "Статус": h["Статус"],
            })
            if line in flagged_lines:
                flagged_item_rows.add(len(items) + 1)  # +1 for the header row

    parties: dict[str, dict] = {}
    for h in headers:
        for name, tax_id, address, role in (
            (h["Поставщик"], h["БИН/ИИН поставщика"], h["Адрес поставщика"], "Поставщик"),
            (h["Покупатель"], h["БИН/ИИН покупателя"], None, "Покупатель"),
        ):
            if not name and not tax_id:
                continue
            key = tax_id or name
            parties.setdefault(key, {"Наименование": name, "БИН/ИИН": tax_id, "Адрес": address, "Роль": role})

    wb = Workbook()
    ws_items = wb.active
    ws_items.title = "Товары и услуги"
    _write_sheet(ws_items, ITEM_COLUMNS, items)
    for row_idx in flagged_item_rows:
        for col_idx in range(1, len(ITEM_COLUMNS) + 1):
            ws_items.cell(row=row_idx, column=col_idx).fill = FLAG_FILL

    ws_docs = wb.create_sheet("Документы")
    _write_sheet(ws_docs, DOC_COLUMNS, headers)
    for row_idx, r in enumerate(results, start=2):
        flagged_cols = {FIELD_COLUMNS[f] for f in r.review_fields if f in FIELD_COLUMNS}
        if r.needs_review:
            flagged_cols |= {"Статус", "Замечания"}
        for col_idx, col in enumerate(DOC_COLUMNS, start=1):
            if col in flagged_cols:
                ws_docs.cell(row=row_idx, column=col_idx).fill = FLAG_FILL

    _write_sheet(wb.create_sheet("Контрагенты"), PARTY_COLUMNS, list(parties.values()))

    ws_help = wb.create_sheet("Инструкция")
    for line in INSTRUCTIONS:
        ws_help.append([line])
    ws_help["A1"].font = Font(bold=True, size=13)
    ws_help.column_dimensions["A"].width = 130
    return neutralize_workbook(wb)


def write(results: list[ExtractionResult], target: Path | BinaryIO) -> list[Path]:
    build_workbook(results).save(target)
    return [target] if isinstance(target, Path) else []


def to_bytes(results: list[ExtractionResult]) -> bytes:
    buffer = BytesIO()
    write(results, buffer)
    return buffer.getvalue()
