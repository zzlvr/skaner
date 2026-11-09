import pytest

from docstruc.validation.kz import is_valid_iin_bin


# Public BINs of large Kazakh banks (company identifiers, not personal data).
@pytest.mark.parametrize("value", ["971240001315", "940140000385"])
def test_valid_bin(value):
    assert is_valid_iin_bin(value)


@pytest.mark.parametrize("value", ["971240001316", "94014000038", "9712400013a5", ""])
def test_invalid_bin(value):
    assert not is_valid_iin_bin(value)


# A 12-digit tax number is a Kazakh IIN/BIN or a Russian individual INN: neither must be flagged as a misread.
def test_russian_inn_is_not_flagged_as_a_bad_kazakh_id():
    from docstruc.doctypes.common import Party
    from docstruc.validation import checks
    from docstruc.validation.ru import is_valid_ru_inn

    assert is_valid_ru_inn("7707083893") and is_valid_ru_inn("500100732259")
    assert not is_valid_ru_inn("7707083894") and not is_valid_ru_inn("500100732258")

    class Doc:
        def __init__(self, tax_id):
            self.seller = Party(tax_id=tax_id)

    check = checks.tax_ids("seller.tax_id")
    assert check(Doc("500100732259")) == []  # valid Russian INN, not a valid IIN/BIN
    assert check(Doc("971240001315")) == []  # valid BIN
    assert [i.field for i in check(Doc("500100732258"))] == ["seller.tax_id"]  # valid in neither
    assert check(Doc("7707083894")) == []  # 10 digits: other countries use such numbers, so not judged
