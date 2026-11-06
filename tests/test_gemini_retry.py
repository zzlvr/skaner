import pytest

from docstruc.llm.gemini import _retry_delay_seconds


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Quota exceeded ... Please retry in 9h25m38.37713142s.", 9 * 3600 + 25 * 60 + 38.37713142),
        ("Please retry in 12.5s.", 12.5),
        ("Please retry in 3m.", 180),
    ],
)
def test_retry_delay_parsing(message, expected):
    assert _retry_delay_seconds(message) == pytest.approx(expected)


def test_no_retry_delay_in_message():
    assert _retry_delay_seconds("Service unavailable") is None
