"""Three languages (en/ru/kk). Texts live in locales/<lang>.json; en is the main language and the fallback."""

import json
from functools import lru_cache
from pathlib import Path

from .config import DEFAULT_LANG, LANGS

_DIR = Path(__file__).parent / "locales"


@lru_cache(maxsize=None)
def _load(lang: str) -> dict:
    return json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def pick_lang(cookie: str | None, accept: str | None = None) -> str:
    """English for everyone; another language only when the visitor chose it with the switcher (the cookie).
    The browser's language header is deliberately ignored."""
    return cookie if cookie in LANGS else DEFAULT_LANG


def translator(lang: str):
    texts, fallback = _load(lang), _load(DEFAULT_LANG)

    def t(key: str) -> str:
        return texts.get(key) or fallback.get(key) or key

    return t
