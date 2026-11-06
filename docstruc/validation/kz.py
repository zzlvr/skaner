"""Kazakhstan-specific validators."""

_WEIGHTS_1 = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
_WEIGHTS_2 = (3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2)


def is_valid_iin_bin(value: str) -> bool:
    """Check the control digit of a 12-digit Kazakh IIN (individuals) or BIN (companies)."""
    if len(value) != 12 or not value.isdigit():
        return False
    digits = [int(c) for c in value]
    control = sum(d * w for d, w in zip(digits, _WEIGHTS_1)) % 11
    if control == 10:
        control = sum(d * w for d, w in zip(digits, _WEIGHTS_2)) % 11
        if control == 10:
            return False
    return control == digits[11]
