"""Checks that compare documents of one batch with each other.

A single document can be perfectly consistent and still be a problem: the same invoice scanned twice is paid twice.
These checks only add warnings: two receipts for the same amount on the same day can be two real purchases.
"""

import re

from docstruc.core.models import ExtractionResult, Issue

DUPLICATE_MESSAGE = "Looks like a duplicate of {other}"

# per document type: (who issued it, the document's own number, its date)
_FIELDS = {
    "receipt": (("merchant_tax_id", "merchant_name"), "receipt_number", "date"),
    "invoice": (("seller.tax_id", "seller.name"), "invoice_number", "invoice_date"),
    "act": (("contractor.tax_id", "contractor.name"), "act_number", "act_date"),
    "waybill": (("sender.tax_id", "sender.name"), "waybill_number", "waybill_date"),
}


def _get(data: dict, path: str):
    value = data
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _norm(value) -> str:
    return re.sub(r"[\W_]+", "", str(value).casefold()) if value not in (None, "") else ""


def _issuer(data: dict, paths: tuple[str, ...]) -> str:
    for path in paths:  # a tax id identifies the issuer better than a name that may be misread
        value = _norm(_get(data, path))
        if value:
            return value
    return ""


def _keys(result: ExtractionResult) -> list[tuple]:
    spec = _FIELDS.get(result.doctype)
    if spec is None or not result.data:
        return []
    issuer_paths, number_path, date_path = spec
    issuer = _issuer(result.data, issuer_paths)
    number = _norm(_get(result.data, number_path))
    date = _get(result.data, date_path)
    total = result.data.get("total")
    keys = []
    if issuer and number:
        keys.append(("number", result.doctype, issuer, number))
    if issuer and date and total is not None:
        keys.append(("amount", result.doctype, issuer, date, round(float(total), 2)))
    return keys


def find_duplicates(results: list[ExtractionResult]) -> list[ExtractionResult]:
    """Warn on every document that repeats an earlier one in the batch (same issuer and number, or same issuer,
    date and total). The first of a group is left alone; the later ones point to it. Returns `results`."""
    seen: dict[tuple, ExtractionResult] = {}
    for result in results:
        original = next((seen[k] for k in _keys(result) if k in seen), None)
        if original is not None:
            message = DUPLICATE_MESSAGE.format(other=original.source)
            if not any(i.message == message for i in result.issues):
                result.issues = [*result.issues, Issue(field="document", severity="warning", message=message)]
            continue
        for key in _keys(result):
            seen.setdefault(key, result)
    return results
