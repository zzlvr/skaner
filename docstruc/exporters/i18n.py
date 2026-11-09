"""Output languages for exported files. Engine data stays language-neutral; only labels are translated."""

import re

LANGUAGES = ("en", "ru")

SHEETS = {
    "ru": {"Documents": "Документы", "Line items": "Позиции", "Review": "Проверка"},
}

COLUMNS = {
    "ru": {
        "file": "Файл", "status": "Статус", "line": "№ строки",
        "merchant_name": "Продавец", "merchant_address": "Адрес продавца", "merchant_tax_id": "БИН/ИИН продавца",
        "receipt_number": "Номер чека", "date": "Дата", "time": "Время", "currency": "Валюта",
        "subtotal": "Сумма без скидки", "discount": "Скидка", "tax_rate": "Ставка налога, %",
        "tax_amount": "Налог (НДС)", "total": "Итого", "payment_method": "Способ оплаты",
        "invoice_number": "Номер счёта", "invoice_date": "Дата счёта", "due_date": "Срок оплаты",
        "seller.name": "Продавец", "seller.address": "Адрес продавца", "seller.tax_id": "БИН/ИИН продавца",
        "buyer.name": "Покупатель", "buyer.address": "Адрес покупателя", "buyer.tax_id": "БИН/ИИН покупателя",
        "act_number": "Номер акта", "act_date": "Дата акта",
        "waybill_number": "Номер накладной", "waybill_date": "Дата накладной",
        "contract_number": "Номер договора", "contract_date": "Дата договора",
        "period_from": "Период с", "period_to": "Период по",
        "contractor.name": "Исполнитель", "contractor.address": "Адрес исполнителя", "contractor.tax_id": "БИН/ИИН исполнителя",
        "customer.name": "Заказчик", "customer.address": "Адрес заказчика", "customer.tax_id": "БИН/ИИН заказчика",
        "sender.name": "Отправитель", "sender.address": "Адрес отправителя", "sender.tax_id": "БИН/ИИН отправителя",
        "recipient.name": "Получатель", "recipient.address": "Адрес получателя", "recipient.tax_id": "БИН/ИИН получателя",
        "unit": "Ед. изм.",
        "description": "Наименование", "quantity": "Количество", "unit_price": "Цена",
        "field": "Поле", "severity": "Важность", "message": "Что проверить",
    },
}

VALUES = {
    "ru": {"ok": "OK", "needs review": "Требует проверки", "error": "проверить", "warning": "для сведения"},
}

DATE_COLUMNS = {
    "date", "invoice_date", "due_date", "act_date", "waybill_date", "contract_date", "period_from", "period_to",
}

ITEM_FIELDS = {"ru": {"description": "наименование", "quantity": "количество", "unit_price": "цена", "total": "сумма",
                      "unit": "ед. изм."}}

_MESSAGES = {
    "ru": [
        (r"Required field is missing", lambda m: "не найдено в документе"),
        (r"Not a valid date: (.*)", lambda m: f"некорректная дата: {m[1]}"),
        (r"Unusual year: (.*)", lambda m: f"необычный год: {m[1]}"),
        (r"quantity × unit price = (\S+), but line total is (\S+)",
         lambda m: f"количество × цена = {m[1]}, а сумма строки {m[2]}"),
        (r"Line items add up to (\S+), but subtotal is (\S+)", lambda m: f"сумма строк {m[1]}, а подытог {m[2]}"),
        (r"Total (\S+) does not match (.*)", lambda m: f"итог {m[1]} не сходится: {_parts_ru(m[2])}"),
        (r"VAT (\S+) is not (\S+)% of (\S+) \(expected (\S+)\)",
         lambda m: f"НДС {m[1]} не равен {m[2]}% от суммы без НДС {m[3]} (ожидалось {m[4]}); "
                   f"если часть позиций без НДС, это нормально"),
        (r"12-digit number fails the IIN/BIN checksum", lambda m: "ИИН/БИН не проходит проверку контрольной цифры"),
        (r"Hard to read on the document, please check", lambda m: "плохо читается в документе, сверьте с оригиналом"),
        (r"Hard to read on the document, but confirmed by (.*)",
         lambda m: f"плохо читается, но подтверждено: {_confirmed_ru(m[1])}"),
        (r"Not found in the document text, please check it against the original",
         lambda m: "не найдено в тексте документа, сверьте с оригиналом"),
        (r"Looks like a duplicate of (.*)", lambda m: f"похоже на дубликат: {m[1]}"),
        (r"No document found in the file", lambda m: "в файле не найден документ"),
        (r"Extraction failed: (.*)", lambda m: f"документ не удалось обработать: {m[1]}"),
    ],
}


def _parts_ru(text: str) -> str:
    for en, ru in (("line items", "сумма строк"), ("subtotal", "подытог"), ("discount", "скидка"), ("tax", "налог")):
        text = text.replace(en, ru)
    return text


def _confirmed_ru(text: str) -> str:
    return {
        "the line items": "суммой строк",
        "quantity × price and the document totals": "количеством × ценой и итогами документа",
        "the IIN/BIN checksum": "контрольной цифрой ИИН/БИН",
    }.get(text, text)


def message(text: str, lang: str) -> str:
    for pattern, render in _MESSAGES.get(lang, []):
        match = re.fullmatch(pattern, text)
        if match:
            return render(match)
    return text


def field(path: str, lang: str, columns: dict[str, str] | None = None) -> str:
    """Human label for an engine field path such as "items.2.total" or "seller.tax_id"."""
    if lang == "en":
        return path
    if path.startswith("items."):
        _, index, name = (path.split(".") + ["", ""])[:3]
        if index.isdigit():
            return f"Строка {int(index) + 1}, {ITEM_FIELDS[lang].get(name, name)}"
    if path == "document":
        return "Документ"
    return (columns or COLUMNS[lang]).get(path, path)


def column(name: str, lang: str) -> str:
    return COLUMNS.get(lang, {}).get(name, name)


def value(text, lang: str):
    return VALUES.get(lang, {}).get(text, text) if isinstance(text, str) else text


def sheet(name: str, lang: str) -> str:
    return SHEETS.get(lang, {}).get(name, name)
