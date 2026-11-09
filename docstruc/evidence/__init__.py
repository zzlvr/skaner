"""Evidence: where on the document each extracted value is printed, and an independent check that it is there.

    locate_evidence(result, parts)   # fills result.evidence and adds "not found in the document" warnings

The model reads the document; this module reads it a second, independent way (the PDF text layer or OCR) and
looks for every value the model returned. A number that is nowhere in the document's text may have been
invented or misread, so it is flagged. OCR itself can misread, so such a flag is a warning to check, not an error.
"""

import re
from collections.abc import Iterator
from datetime import date

from docstruc.core.models import Document, Evidence, ExtractionResult, Issue
from docstruc.evidence import match
from docstruc.evidence.pages import Page, Word, ocr_available, read_pages

__all__ = ["Page", "Word", "locate_evidence", "ocr_available", "read_pages"]

NOT_FOUND_MESSAGE = "Not found in the document text, please check it against the original"
_SKIP = {"currency", "uncertain_fields"}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Values whose absence from the text is worth a warning: amounts, dates and identifiers.
_STRICT_SUFFIXES = ("tax_id", "_number", "iban", "_date", "date")
MIN_WORDS_FOR_NOT_FOUND = 8  # on a page with almost no readable text, a miss says nothing


def _walk(node, path: str = "") -> Iterator[tuple[str, object]]:
    """Every leaf of the result data with its dotted path, in document order."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in _SKIP:
                yield from _walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for n, value in enumerate(node):
            yield from _walk(value, f"{path}.{n}" if path else str(n))
    elif node is not None and not isinstance(node, bool) and node != "":
        yield path, node


def _is_strict(path: str, value: object) -> bool:
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and (bool(_ISO_DATE.match(value)) or path.rsplit(".", 1)[-1].endswith(_STRICT_SUFFIXES))


def _implied_by_line(data: dict, path: list[str]) -> bool:
    """A line amount the OCR misread is still confirmed when quantity × unit price gives it exactly."""
    if path[0] != "items" or len(path) != 3 or path[2] not in ("total", "unit_price", "quantity"):
        return False
    try:
        line = data["items"][int(path[1])]
        qty, price, total = line.get("quantity"), line.get("unit_price"), line.get("total")
    except (KeyError, IndexError, ValueError):
        return False
    return None not in (qty, price, total) and abs(qty * price - total) <= 0.011


def _hits(pages: list[Page], value: object) -> list[match.Hit]:
    hits: list[match.Hit] = []
    for page in pages:
        if isinstance(value, (int, float)):
            hits += match.number_hits(page, float(value))
        elif isinstance(value, str) and _ISO_DATE.match(value):
            try:
                hits += match.date_hits(page, date.fromisoformat(value))
            except ValueError:
                pass
        elif isinstance(value, str):
            hits += match.text_hits(page, value)
    return hits


def locate_evidence(result: ExtractionResult, parts: list[Document]) -> ExtractionResult:
    """Fill `result.evidence` and warn about values that cannot be found in the document. Returns `result`."""
    pages = read_pages(parts)
    if sum(len(p.words) for p in pages) < MIN_WORDS_FOR_NOT_FOUND:
        return result  # no text layer and no OCR: nothing to compare with

    used: set[tuple[int, int, int]] = set()
    found: dict[str, match.Hit] = {}
    evidence: dict[str, Evidence] = {}
    flagged = result.flagged_fields
    warnings: list[Issue] = []
    for path, value in _walk(result.data):
        parts_of_path = path.split(".")
        anchor = None
        if parts_of_path[0] == "items" and len(parts_of_path) > 2:
            anchor = found.get(f"items.{parts_of_path[1]}.description")
        hits = _hits(pages, value)
        hit = match.pick(hits, used, anchor, prefer_last=parts_of_path[-1].startswith("total") and len(parts_of_path) == 1)
        if hit is None:
            if _is_strict(path, value) and path not in flagged and not _implied_by_line(result.data, parts_of_path):
                warnings.append(Issue(field=path, severity="warning", message=NOT_FOUND_MESSAGE))
            continue
        used.add(match._id(hit))
        found[path] = hit
        evidence[path] = Evidence(part=hit.page.part, page=hit.page.index, box=hit.box, text=hit.text)
    result.evidence = evidence
    result.issues = [*result.issues, *warnings]
    return result
