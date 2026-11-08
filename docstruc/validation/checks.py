"""Reusable validators. Each returns a list of `Issue`; an empty list means the check passed.

These checks are what makes a result trustworthy: a human only needs to look at flagged fields.
"""

import re
from datetime import date
from typing import Any

from pydantic import BaseModel

from docstruc.core.models import Issue
from docstruc.validation.kz import is_valid_iin_bin

# Amounts are printed rounded to cents, so allow a cent per added term. No relative slack: 0.1% of a
# 4 000 000 ₸ invoice is 4 000 ₸, so a misread digit in the hundreds would pass unnoticed.
ABS_TOLERANCE = 0.011
REL_TOLERANCE = 1e-9  # float noise only
# A printed unit price is rounded to cents, so quantity × printed price may differ from the line total
# by up to half a cent per unit.
PRICE_ROUNDING = 0.005


def _get(data: BaseModel, path: str) -> Any:
    """Read a dotted path such as "seller.tax_id" or "items.2.total"."""
    value: Any = data
    for part in path.split("."):
        if isinstance(value, list):
            value = value[int(part)] if part.isdigit() and int(part) < len(value) else None
        else:
            value = getattr(value, part, None)
        if value is None:
            return None
    return value


def _close(a: float, b: float, terms: int = 1) -> bool:
    return abs(a - b) <= max(ABS_TOLERANCE * terms, REL_TOLERANCE * max(abs(a), abs(b)))


UNCERTAIN_MESSAGE = "Hard to read on the document, please check"
CONFIRMED_MESSAGE = "Hard to read on the document, but confirmed by {by}"

# Free-text fields: a misread here is cosmetic, so doubt is only highlighted, not sent to review.
_COSMETIC_FIELDS = ("description", "name", "address", "payment_method", "notes")


def model_uncertain(data: BaseModel) -> list[Issue]:
    """Fields the model itself reported as hard to read: the main signal for silent misreads.

    Doubt about amounts, quantities, dates and IDs requires review; doubt about free text is a warning.
    """
    issues = []
    for f in dict.fromkeys(getattr(data, "uncertain_fields", []) or []):
        cosmetic = f.rsplit(".", 1)[-1].endswith(_COSMETIC_FIELDS)
        issues.append(
            Issue(
                field=f,
                severity="warning" if cosmetic else "error",
                message=UNCERTAIN_MESSAGE,
            )
        )
    return issues


def corroborate(data: BaseModel, issues: list[Issue]) -> list[Issue]:
    """Downgrade the model's doubts that the document's own arithmetic or checksums confirm.

    A doubted line value is confirmed when quantity × unit price = line total and the document
    totals add up; a doubted 12-digit Kazakh IIN/BIN is confirmed by a valid control digit
    (the IIN/BIN checksum always detects a single wrong digit). Doubts nothing can confirm,
    such as dates, still require review.
    """
    check_errors = {i.field for i in issues if i.severity == "error" and i.message != UNCERTAIN_MESSAGE}
    confirmed: dict[str, str] = {}

    items = getattr(data, "items", []) or []
    totals_ok = (
        _items_total(data) is not None
        and getattr(data, "total", None) is not None
        and not {"total", "subtotal"} & check_errors
    )
    if totals_ok:
        for name in ("total", "subtotal", "discount"):
            if getattr(data, name, None) is not None:
                confirmed[name] = "the line items"
        for i, item in enumerate(items):
            if None not in (item.quantity, item.unit_price, item.total) and f"items.{i}.total" not in check_errors:
                for name in ("quantity", "unit_price", "total"):
                    confirmed[f"items.{i}.{name}"] = "quantity × price and the document totals"

    result = []
    for issue in issues:
        by = confirmed.get(issue.field)
        if by is None and issue.message == UNCERTAIN_MESSAGE:
            value = _get(data, issue.field)
            if isinstance(value, str) and _TWELVE_DIGITS.match(re.sub(r"\s", "", value)):
                if is_valid_iin_bin(re.sub(r"\s", "", value)):
                    by = "the IIN/BIN checksum"
        if by is not None and issue.message == UNCERTAIN_MESSAGE and issue.severity == "error":
            issue = Issue(field=issue.field, severity="warning", message=CONFIRMED_MESSAGE.format(by=by))
        result.append(issue)
    return result


def required(*fields: str):
    def check(data: BaseModel) -> list[Issue]:
        return [
            Issue(field=f, severity="error", message="Required field is missing")
            for f in fields
            if _get(data, f) in (None, "")
        ]

    return check


def valid_date(*fields: str):
    def check(data: BaseModel) -> list[Issue]:
        issues = []
        for f in fields:
            value = _get(data, f)
            if value is None:
                continue
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                issues.append(Issue(field=f, severity="error", message=f"Not a valid date: {value!r}"))
                continue
            if not 1990 <= parsed.year <= date.today().year + 2:
                issues.append(Issue(field=f, severity="warning", message=f"Unusual year: {parsed.year}"))
        return issues

    return check


def line_totals(data: BaseModel) -> list[Issue]:
    """quantity * unit_price should equal the line total."""
    issues = []
    for i, item in enumerate(getattr(data, "items", [])):
        if None in (item.quantity, item.unit_price, item.total):
            continue
        expected = item.quantity * item.unit_price
        diff = abs(expected - item.total)
        # Weighted goods are often rounded to a whole currency unit (e.g. 1.929 kg × 161 = 310.57 → 311);
        # for whole quantities that slack would hide a misread digit, so it applies to fractional ones only.
        weighted = item.quantity != int(item.quantity)
        if not (diff <= ABS_TOLERANCE + PRICE_ROUNDING * abs(item.quantity)
                or (weighted and diff <= 0.5 + ABS_TOLERANCE)):
            issues.append(
                Issue(
                    field=f"items.{i}.total",
                    severity="error",
                    message=f"quantity × unit price = {expected:.2f}, but line total is {item.total:.2f}",
                )
            )
    return issues


def _items_total(data: BaseModel) -> float | None:
    items = getattr(data, "items", [])
    if not items or any(item.total is None for item in items):
        return None
    return sum(item.total for item in items)


def items_sum(data: BaseModel) -> list[Issue]:
    """Line totals should add up to the subtotal (the amount before discounts)."""
    items_total, subtotal = _items_total(data), getattr(data, "subtotal", None)
    if items_total is None or subtotal is None:
        return []
    terms = len(data.items)
    if _close(items_total, subtotal, terms=terms):
        return []
    # Some documents print a subtotal that already includes tax.
    tax = getattr(data, "tax_amount", None)
    if tax is not None and _close(items_total + tax, subtotal, terms=terms):
        return []
    # Others print line amounts with VAT next to a subtotal without it («Итого без НДС»).
    if tax is not None and _close(items_total - tax, subtotal, terms=terms):
        return []
    # Others print line prices after discount next to a subtotal before discount.
    discount = getattr(data, "discount", None)
    if discount and _close(items_total, subtotal - discount, terms=terms):
        return []
    return [
        Issue(
            field="subtotal",
            severity="error",
            message=f"Line items add up to {items_total:.2f}, but subtotal is {subtotal:.2f}",
        )
    ]


def total_consistent(data: BaseModel) -> list[Issue]:
    """total = (subtotal or sum of items) - discount, plus tax unless prices already include it."""
    total = getattr(data, "total", None)
    subtotal = getattr(data, "subtotal", None)
    base = subtotal if subtotal is not None else _items_total(data)
    if total is None or base is None:
        return []
    discount = getattr(data, "discount", None) or 0
    tax = getattr(data, "tax_amount", None) or 0
    terms = len(getattr(data, "items", [])) + 2
    candidates = [base - discount, base - discount + tax]
    if any(_close(c, total, terms=terms) for c in candidates):
        return []
    source = "subtotal" if subtotal is not None else "line items"
    parts = [f"{source} {base:.2f}"]
    if discount:
        parts.append(f"discount {discount:.2f}")
    if tax:
        parts.append(f"tax {tax:.2f}")
    return [
        Issue(
            field="total",
            severity="error",
            message=f"Total {total:.2f} does not match {', '.join(parts)}",
        )
    ]


def tax_consistent(data: BaseModel) -> list[Issue]:
    """The VAT amount should be the rate applied to the amount without VAT (total - VAT).

    Holds both when VAT is added on top and when prices already include it ("в том числе НДС").
    VAT is often rounded per line, so a few tiyn per line are allowed. Without this check a misread
    VAT amount (or rate) passes every other check, because the total does not depend on it.
    """
    tax, rate, total = (getattr(data, n, None) for n in ("tax_amount", "tax_rate", "total"))
    if tax is None or not rate or total is None or tax <= 0 or total <= tax:
        return []
    # The amount without VAT, from every base the document gives: when only the total (or only the
    # subtotal) is misread, the VAT itself is still confirmed and is not flagged together with it.
    subtotal, discount, items = getattr(data, "subtotal", None), getattr(data, "discount", None) or 0, _items_total(data)
    bases = [total]
    if subtotal is not None:
        bases += [subtotal, subtotal - discount]
    if items is not None:
        bases += [items, items - discount]
    nets = [b - t for b in bases for t in (tax, 0)]
    expected = (total - tax) * rate / 100
    lines = len(getattr(data, "items", []) or [])
    tolerance = ABS_TOLERANCE * 2 + PRICE_ROUNDING * lines
    if any(abs(n * rate / 100 - tax) <= tolerance for n in nets):
        return []
    return [
        Issue(
            field="tax_amount",
            severity="error",
            message=f"VAT {tax:.2f} is not {rate:g}% of {total - tax:.2f} (expected {expected:.2f})",
        )
    ]


_TWELVE_DIGITS = re.compile(r"^\d{12}$")


def kz_tax_ids(*fields: str):
    """If a tax id looks like a Kazakh IIN/BIN (12 digits), verify its control digit."""

    def check(data: BaseModel) -> list[Issue]:
        issues = []
        for f in fields:
            value = _get(data, f)
            if not value:
                continue
            compact = re.sub(r"\s", "", value)
            if _TWELVE_DIGITS.match(compact) and not is_valid_iin_bin(compact):
                # A failed checksum almost always means a misread digit, so a human must check it.
                issues.append(
                    Issue(field=f, severity="error", message="12-digit number fails the IIN/BIN checksum")
                )
        return issues

    return check
