from docstruc.core.models import ExtractionResult
from docstruc.exporters import i18n
from docstruc.validation.batch import find_duplicates


def _invoice(source, number="INV-1", tax_id="123456789012", date="2026-10-03", total=100.0):
    return ExtractionResult(
        source=source, doctype="invoice", model="fake",
        data={"invoice_number": number, "invoice_date": date, "total": total, "seller": {"tax_id": tax_id, "name": "Acme"}},
    )


def _messages(result):
    return [i.message for i in result.issues]


def test_same_issuer_and_number_is_a_duplicate_even_with_a_different_amount():
    a, b = _invoice("a.pdf"), _invoice("scan 2.pdf", number="inv 1", total=105.0)  # number written differently
    find_duplicates([a, b])
    assert _messages(a) == []  # the first one is not accused
    assert _messages(b) == ["Looks like a duplicate of a.pdf"]
    assert b.issues[0].severity == "warning" and not b.needs_review


def test_same_issuer_date_and_total_without_a_number():
    a = _invoice("a.pdf", number=None)
    b = _invoice("b.pdf", number=None)
    c = _invoice("c.pdf", number=None, total=99.0)
    find_duplicates([a, b, c])
    assert _messages(b) == ["Looks like a duplicate of a.pdf"] and _messages(c) == []


def test_different_issuers_or_numbers_are_not_duplicates():
    docs = [_invoice("a.pdf"), _invoice("b.pdf", number="INV-2", total=50.0), _invoice("c.pdf", tax_id="999999999999")]
    find_duplicates(docs)
    assert all(_messages(d) == [] for d in docs)


def test_a_third_copy_points_to_the_first_and_warnings_are_not_added_twice():
    docs = [_invoice("a.pdf"), _invoice("b.pdf"), _invoice("c.pdf")]
    find_duplicates(docs)
    find_duplicates(docs)
    assert _messages(docs[1]) == _messages(docs[2]) == ["Looks like a duplicate of a.pdf"]


def test_failed_and_empty_results_are_ignored_and_the_message_is_translated():
    failed = ExtractionResult.failed("x.pdf", "invoice", "boom")
    a, b = _invoice("a.pdf"), _invoice("b.pdf")
    find_duplicates([failed, a, b])
    assert len(failed.issues) == 1
    assert i18n.message(b.issues[0].message, "ru") == "похоже на дубликат: a.pdf"
