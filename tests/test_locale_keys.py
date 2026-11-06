"""A translation key defined twice silently takes the LAST value: catch it."""

import collections
import json
from pathlib import Path

import pytest

LOCALES = Path(__file__).parent.parent / "web" / "locales"


@pytest.mark.parametrize("lang", ["ru", "kk", "en"])
def test_no_duplicate_keys(lang):
    pairs = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"), object_pairs_hook=lambda p: p)
    dupes = [k for k, n in collections.Counter(k for k, _ in pairs).items() if n > 1]
    assert dupes == []
