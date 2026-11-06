import pytest

from docstruc.validation.kz import is_valid_iin_bin


# Public BINs of large Kazakh banks (company identifiers, not personal data).
@pytest.mark.parametrize("value", ["971240001315", "940140000385"])
def test_valid_bin(value):
    assert is_valid_iin_bin(value)


@pytest.mark.parametrize("value", ["971240001316", "94014000038", "9712400013a5", ""])
def test_invalid_bin(value):
    assert not is_valid_iin_bin(value)
