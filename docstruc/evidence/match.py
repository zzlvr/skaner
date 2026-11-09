"""Finding where each extracted value is printed on the page.

Matching is by value, not by text: "1 200", "1200,00" and "1.200" all match the number 1200; "14.10.2026" and
"14/10/26" both match the date 2026-10-14. When a value is printed several times the choice is a heuristic (a line
item's amounts are taken next to its description, a document total from the last place it appears), so a box is a
strong hint and not a proof.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher

from docstruc.evidence.pages import Page, Word

NUMBER_TOLERANCE = 0.005
FUZZY_RATIO = 0.85
MAX_RUN = 3  # words joined to read one number or date ("1 200", "3 Oct 2026")

_MONTHS = {
    **{m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)},
    **{m: i for i, m in enumerate(("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"), 1)},
    **{m: i for i, m in enumerate(("қаң", "ақп", "нау", "сәу", "мам", "мау", "шіл", "там", "қыр", "қаз", "қар", "жел"), 1)},
}
_NUM_WORD = re.compile(r"[-−]?\d[\d.,]*")
_THOUSAND_HEAD = re.compile(r"^\d{1,3}$")
_THOUSAND_TAIL = re.compile(r"^\d{3}(?:[.,]\d{1,2})?$")
_DATE_NUMERIC = re.compile(r"(?<!\d)(\d{1,4})[./\-](\d{1,2})[./\-](\d{2,4})(?!\d)")
_DATE_TEXT_DMY = re.compile(r"(?<!\d)(\d{1,2})[\s.,-]*([^\W\d_]{3,})\.?[\s,]*(\d{2,4})(?!\d)")
_DATE_TEXT_MDY = re.compile(r"([^\W\d_]{3,})\.?[\s,]*(\d{1,2})(?:st|nd|rd|th)?[\s,]+(\d{4})(?!\d)")


@dataclass(frozen=True)
class Hit:
    page: Page
    words: tuple[Word, ...]
    text: str

    @property
    def box(self) -> tuple[float, float, float, float]:
        return (
            min(w.box[0] for w in self.words),
            min(w.box[1] for w in self.words),
            max(w.box[2] for w in self.words),
            max(w.box[3] for w in self.words),
        )

    @property
    def y(self) -> float:
        b = self.box
        return (b[1] + b[3]) / 2


def numbers_in(text: str) -> set[float]:
    """The numbers a printed token can mean ("1,200" is 1200 or 1.2; "586,21" is 586.21)."""
    match = _NUM_WORD.search(text.replace("−", "-"))
    if not match:
        return set()
    token = match.group(0).rstrip(".,")
    negative = token.startswith("-")
    token = token.lstrip("-")
    if not token:
        return set()
    values: set[float] = set()
    seps = [c for c in token if c in ".,"]
    try:
        if not seps:
            values.add(float(token))
        elif len(seps) == 1:
            head, tail = token.replace(",", ".").split(".")
            values.add(float(f"{head or 0}.{tail}"))
            if len(tail) == 3:
                values.add(float(head + tail))  # "1,200" may be a thousands separator
        else:
            last = max(token.rfind("."), token.rfind(","))
            head, tail = token[:last], token[last + 1 :]
            digits = re.sub(r"[.,]", "", head)
            if len(tail) == 3 and token.count(token[last]) == len(seps):
                values.add(float(digits + tail))  # "1.234.567": only thousands separators
            elif len(set(seps)) > 1:
                values.add(float(f"{digits}.{tail}"))
    except ValueError:
        return set()
    return {-v if negative else v for v in values}


def _runs(words: list[Word], size: int) -> Iterator[tuple[int, int]]:
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + size) + 1):
            yield i, j


def _number_candidates(page: Page) -> list[tuple[Hit, set[float]]]:
    words = page.words
    out: list[tuple[Hit, set[float]]] = []
    for i, word in enumerate(words):
        own = numbers_in(word.text)
        if own:
            out.append((Hit(page, (word,), word.text), own))
        if not _THOUSAND_HEAD.match(word.text):
            continue
        joined, run = word.text, [word]
        for nxt in words[i + 1 : i + MAX_RUN]:
            if not _THOUSAND_TAIL.match(nxt.text) or abs(_center_y(nxt) - _center_y(word)) > _height(word):
                break
            joined += nxt.text
            run.append(nxt)
            values = numbers_in(joined)
            if values:
                out.append((Hit(page, tuple(run), " ".join(w.text for w in run)), values))
    return out


def _center_y(word: Word) -> float:
    return (word.box[1] + word.box[3]) / 2


def _height(word: Word) -> float:
    return word.box[3] - word.box[1]


def number_hits(page: Page, value: float) -> list[Hit]:
    return [h for h, vals in _number_candidates(page) if any(abs(v - value) <= NUMBER_TOLERANCE for v in vals)]


def _expand_year(y: int) -> int:
    return 2000 + y if y < 100 else y


def _dates_in(text: str) -> set[date]:
    found: set[date] = set()

    def add(y: int, m: int, d: int) -> None:
        try:
            found.add(date(_expand_year(y), m, d))
        except ValueError:
            pass

    for a, b, c in ((int(x), int(y), int(z)) for x, y, z in _DATE_NUMERIC.findall(text)):
        if a > 31:  # year first
            add(a, b, c)
        else:
            add(c, b, a)  # day.month.year
            add(c, a, b)  # month/day/year
    for d, name, y in _DATE_TEXT_DMY.findall(text):
        if name[:3].casefold() in _MONTHS:
            add(int(y), _MONTHS[name[:3].casefold()], int(d))
    for name, d, y in _DATE_TEXT_MDY.findall(text):
        if name[:3].casefold() in _MONTHS:
            add(int(y), _MONTHS[name[:3].casefold()], int(d))
    return found


def date_hits(page: Page, value: date) -> list[Hit]:
    words = page.words
    taken: set[int] = set()
    hits = []
    # shortest runs first, and a run may not reuse a word of an accepted one: "10/12/2026" beats "#A-20931 10/12/2026"
    for size in range(1, MAX_RUN + 1):
        for i in range(len(words) - size + 1):
            if taken.intersection(range(i, i + size)):
                continue
            text = " ".join(w.text for w in words[i : i + size])
            if value in _dates_in(text):
                taken.update(range(i, i + size))
                hits.append(Hit(page, tuple(words[i : i + size]), text))
    hits.sort(key=lambda h: (h.box[1], h.box[0]))
    return hits


def _key(text: str) -> str:
    return "".join(c for c in text.casefold() if c.isalnum())


def text_hits(page: Page, value: str) -> list[Hit]:
    """Places where the text is printed. Exact matches only when there are any, otherwise the best misreads."""
    key = _key(value)
    if len(key) < 3:
        return []
    words = page.words
    keyed = [_key(w.text) for w in words]
    wanted = max(1, len(value.split()))
    contains_ok = len(key) >= 5 and any(c.isdigit() for c in key)  # ids glued to a label: "БИН261010000123"
    exact: list[Hit] = []
    fuzzy: list[tuple[float, Hit]] = []
    for i in range(len(words)):
        joined = ""
        best: tuple[float, int] | None = None
        for j in range(i, min(len(words), i + wanted + 2)):
            joined += keyed[j]
            if not joined:
                continue
            if joined == key or (contains_ok and key in joined and j == i):
                exact.append(Hit(page, tuple(words[i : j + 1]), " ".join(w.text for w in words[i : j + 1])))
                best = None
                break
            if len(key) >= 4 and abs(len(joined) - len(key)) <= max(2, len(key) // 4):
                matcher = SequenceMatcher(None, joined, key)
                if matcher.real_quick_ratio() >= FUZZY_RATIO and matcher.ratio() >= FUZZY_RATIO:
                    if best is None or matcher.ratio() > best[0]:
                        best = (matcher.ratio(), j)
            if len(joined) > len(key) * 1.5 + 2:
                break
        if best is not None:
            fuzzy.append((best[0], Hit(page, tuple(words[i : best[1] + 1]), " ".join(w.text for w in words[i : best[1] + 1]))))
    if exact:
        return exact
    fuzzy.sort(key=lambda t: -t[0])
    return [h for _r, h in fuzzy[:3]]


def pick(hits: list[Hit], used: set[tuple[int, int, int]], anchor: Hit | None, prefer_last: bool) -> Hit | None:
    """One hit among several: unused first; near the anchor when there is one; the last one for totals."""
    fresh = [h for h in hits if _id(h) not in used] or hits
    if not fresh:
        return None
    if anchor is not None:
        same_page = [h for h in fresh if h.page is anchor.page] or fresh
        return min(same_page, key=lambda h: abs(h.y - anchor.y))
    return fresh[-1] if prefer_last else fresh[0]


def _id(hit: Hit) -> tuple[int, int, int]:
    return hit.page.part, hit.page.index, id(hit.words[0])
