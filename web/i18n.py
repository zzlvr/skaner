"""Three languages (en/ru/kk). Texts live in locales/<lang>.json; en is the main language and the fallback."""

import json
from functools import lru_cache
from pathlib import Path

from .config import DEFAULT_LANG, LANGS

_DIR = Path(__file__).parent / "locales"


@lru_cache(maxsize=None)
def _load(lang: str) -> dict:
    return json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def pick_lang(cookie: str | None, accept: str | None) -> str:
    if cookie in LANGS:
        return cookie
    for part in (accept or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
        if code == "ru":
            return "ru"
    return DEFAULT_LANG


def translator(lang: str):
    texts, fallback = _load(lang), _load(DEFAULT_LANG)

    def t(key: str) -> str:
        return texts.get(key) or fallback.get(key) or key

    return t
