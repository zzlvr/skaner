from docstruc.doctypes.common import LineItem, Party
from docstruc.doctypes.invoice import INVOICE, Invoice
from docstruc.doctypes.receipt import RECEIPT, Receipt


def fields(issues):
    return {i.field for i in issues}


def good_receipt(**overrides):
    data = dict(
        merchant_name="ACME",
        date="2026-10-01",
        items=[
            LineItem(description="Coffee", quantity=2, unit_price=3.5, total=7.0),
            LineItem(description="Bagel", quantity=1, unit_price=2.25, total=2.25),
        ],
        subtotal=9.25,
        tax_amount=0.74,
        total=9.99,
    )
    data.update(overrides)
    return Receipt(**data)


def test_consistent_receipt_has_no_issues():
    assert RECEIPT.validate(good_receipt()) == []


def test_missing_required_fields():
    issues = RECEIPT.validate(Receipt())
    assert {"total", "date"} <= fields(issues)
    assert all(i.severity == "error" for i in issues)


def test_bad_date():
    assert "date" in fields(RECEIPT.validate(good_receipt(date="01/10/2026")))


def test_line_total_mismatch():
    items = [LineItem(description="Coffee", quantity=2, unit_price=3.5, total=8.0)]
    issues = RECEIPT.validate(good_receipt(items=items, subtotal=8.0, tax_amount=None, total=8.0))
    assert fields(issues) == {"items.0.total"}


def test_items_do_not_add_up_to_subtotal():
    assert "subtotal" in fields(RECEIPT.validate(good_receipt(subtotal=10.25, total=10.99)))


def test_total_mismatch():
    assert fields(RECEIPT.validate(good_receipt(total=12.00))) == {"total"}


def test_tax_included_prices_are_accepted():
    # Prices already include tax; tax is printed for information only (typical for Kazakh receipts).
    assert RECEIPT.validate(good_receipt(subtotal=9.25, tax_amount=0.99, total=9.25)) == []
    assert RECEIPT.validate(good_receipt(subtotal=None, tax_amount=0.99, total=9.25)) == []


def test_items_against_total_without_subtotal():
    assert RECEIPT.validate(good_receipt(subtotal=None, tax_amount=None, total=9.25)) == []
    assert "total" in fields(RECEIPT.validate(good_receipt(subtotal=None, tax_amount=None, total=20)))


def test_discount_is_taken_into_account():
    # Items 9.25, discount 1.25, tax included: pay 8.00.
    assert RECEIPT.validate(good_receipt(subtotal=9.25, discount=1.25, tax_amount=0.86, total=8.00)) == []
    assert RECEIPT.validate(good_receipt(subtotal=None, discount=1.25, tax_amount=None, total=8.00)) == []
    # Without the discount field the same total is flagged.
    assert fields(RECEIPT.validate(good_receipt(subtotal=9.25, tax_amount=0.86, total=8.00))) == {"total"}


def test_invoice_kz_bin_checksum_failure_needs_review():
    invoice = Invoice(
        invoice_number="17",
        invoice_date="2026-09-30",
        seller=Party(name="Seller LLP", tax_id="971240001316"),
        buyer=Party(name="Buyer LLP", tax_id="940140000385"),
        total=100,
    )
    issues = INVOICE.validate(invoice)
    assert [(i.field, i.severity) for i in issues] == [("seller.tax_id", "error")]


def test_non_kz_tax_id_is_not_checked():
    invoice = Invoice(invoice_number="A1", invoice_date="2026-09-30", seller=Party(tax_id="DE123456789"), total=5)
    assert INVOICE.validate(invoice) == []


def test_weighted_line_rounded_to_whole_unit_is_accepted():
    items = [LineItem(description="Potatoes, kg", quantity=1.929, unit_price=161, total=311)]
    assert RECEIPT.validate(good_receipt(items=items, subtotal=None, tax_amount=None, total=311)) == []
    items = [LineItem(description="Potatoes, kg", quantity=1.929, unit_price=161, total=312)]
    assert "items.0.total" in fields(RECEIPT.validate(good_receipt(items=items, subtotal=None, tax_amount=None, total=312)))


def test_line_prices_after_discount_with_subtotal_before_discount():
    # Items are printed after discount (9.25); subtotal before discount is 10.50, discount 1.25.
    assert RECEIPT.validate(good_receipt(subtotal=10.50, discount=1.25, tax_amount=1.0, total=9.25)) == []


def test_fields_reported_uncertain_need_review():
    issues = RECEIPT.validate(good_receipt(uncertain_fields=["date", "date", "tax_amount"]))
    assert [(i.field, i.severity) for i in issues] == [("date", "error"), ("tax_amount", "error")]


def test_uncertain_free_text_is_only_a_warning():
    fields_ = ["items.1.description", "merchant_name", "merchant_address", "payment_method", "merchant_tax_id"]
    issues = RECEIPT.validate(good_receipt(uncertain_fields=fields_))
    assert [i.severity for i in issues] == ["warning", "warning", "warning", "warning", "error"]


def test_doubts_confirmed_by_arithmetic_and_checksum_are_downgraded():
    receipt = good_receipt(
        merchant_tax_id="261010000123",
        uncertain_fields=["items.0.quantity", "items.1.unit_price", "total", "merchant_tax_id", "date"],
    )
    severities = {i.field: i.severity for i in RECEIPT.validate(receipt)}
    assert severities == {
        "items.0.quantity": "warning",
        "items.1.unit_price": "warning",
        "total": "warning",
        "merchant_tax_id": "warning",
        "date": "error",  # nothing on the document can confirm a date
    }


def test_doubts_are_kept_when_the_arithmetic_fails():
    items = [LineItem(description="Coffee", quantity=2, unit_price=3.5, total=8.0)]
    receipt = good_receipt(items=items, subtotal=None, tax_amount=None, total=8.0, uncertain_fields=["items.0.quantity"])
    severities = {i.field: i.severity for i in RECEIPT.validate(receipt)}
    assert severities["items.0.quantity"] == "error"


def test_doubted_iin_with_bad_checksum_stays_an_error():
    receipt = good_receipt(merchant_tax_id="341019451120", uncertain_fields=["merchant_tax_id"])
    assert {i.severity for i in RECEIPT.validate(receipt) if i.field == "merchant_tax_id"} == {"error"}


# ---------- QA 2026-10-25: misreads in large tenge amounts and VAT must not pass silently ----------

def kz_invoice(**overrides):
    """A VAT-included Kazakh invoice (311 520,00 ₸, «в том числе НДС 12%: 33 377,14»), as in the QA corpus."""
    data = dict(
        invoice_number="181", invoice_date="2026-08-16", currency="KZT", tax_rate=12, tax_amount=33377.14, total=311520.0,
        items=[
            LineItem(description="Монитор", quantity=2, unit_price=64990, total=129980),
            LineItem(description="Клавиатура", quantity=5, unit_price=6490, total=32450),
            LineItem(description="Тонер-картридж", quantity=6, unit_price=24600, total=147600),
            LineItem(description="Вода", quantity=1, unit_price=1100, total=1100),
            LineItem(description="Сахар", quantity=0.75, unit_price=520, total=390),
        ],
    )
    data.update(overrides)
    return Invoice(**data)


def test_consistent_kz_invoice_has_no_issues():
    assert INVOICE.validate(kz_invoice()) == []
    # VAT on top: 278 142,86 + 12% = 311 520,00
    assert INVOICE.validate(kz_invoice(subtotal=278142.86, tax_amount=33377.14)) == []


def test_misread_hundreds_in_a_large_total_is_flagged():
    # 300 ₸ off on 311 520 ₸ is under 0.1%: the old relative slack let it pass.
    assert "total" in fields(INVOICE.validate(kz_invoice(total=311820.0)))
    assert "items.2.total" in fields(INVOICE.validate(kz_invoice(items=[
        *kz_invoice().items[:2], LineItem(description="Тонер-картридж", quantity=6, unit_price=24600, total=147680),
        *kz_invoice().items[3:]])))


def test_misread_vat_or_rate_is_flagged():
    assert "tax_amount" in fields(INVOICE.validate(kz_invoice(tax_amount=38377.14)))
    assert "tax_amount" in fields(INVOICE.validate(kz_invoice(tax_rate=16)))


def test_vat_check_tolerates_per_line_rounding_and_skips_without_rate():
    assert INVOICE.validate(kz_invoice(tax_amount=33377.16)) == []  # VAT rounded line by line
    assert INVOICE.validate(kz_invoice(tax_rate=None, tax_amount=99.0)) == []


def test_wrong_total_does_not_also_blame_a_correct_vat():
    assert fields(INVOICE.validate(kz_invoice(total=311820.0, subtotal=311520.0))) == {"total"}


def test_weighted_goods_rounding_still_allowed_but_not_for_whole_quantities():
    weighed = [LineItem(description="Сыр", quantity=1.929, unit_price=161, total=311)]
    assert "items.0.total" not in fields(RECEIPT.validate(good_receipt(items=weighed, subtotal=None, tax_amount=None, total=311)))
    whole = [LineItem(description="Хлеб", quantity=2, unit_price=150.2, total=300.0)]
    assert "items.0.total" in fields(RECEIPT.validate(good_receipt(items=whole, subtotal=None, tax_amount=None, total=300.0)))
    # A printed price rounded to tiyn: 1000 × 18.46 vs 18 456.70 is within half a tiyn per unit.
    bulk = [LineItem(description="Саморез", quantity=1000, unit_price=18.46, total=18456.7)]
    assert "items.0.total" not in fields(RECEIPT.validate(good_receipt(items=bulk, subtotal=None, tax_amount=None, total=18456.7)))
