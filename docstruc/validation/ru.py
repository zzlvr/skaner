"""Russian tax number (INN) checksum. A company INN has 10 digits, an individual's has 12."""

_W10 = (2, 4, 10, 3, 5, 9, 4, 6, 8)
_W11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
_W12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)


def _control(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(d * w for d, w in zip(digits, weights)) % 11 % 10


def is_valid_ru_inn(value: str) -> bool:
    if not value.isdigit() or len(value) not in (10, 12):
        return False
    digits = [int(c) for c in value]
    if len(digits) == 10:
        return _control(digits, _W10) == digits[9]
    return _control(digits, _W11) == digits[10] and _control(digits, _W12) == digits[11]
