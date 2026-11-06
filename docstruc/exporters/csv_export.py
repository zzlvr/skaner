"""CSV export: `name.csv` for documents and `name_items.csv` for line items (UTF-8 with BOM for Excel).

Excel opens a CSV with the separators of the computer's region. In Russian and Kazakh Windows these are ";"
between values and "," in decimals: a comma-separated file lands in one column there, and a value such as 12.5
can turn into a date. So the Russian-language file uses ";" and decimal commas, the English one "," and points.
"""

import csv
import io
from pathlib import Path

from docstruc.core.models import ExtractionResult
from docstruc.exporters.safe import csv_value
from docstruc.exporters.tabular import document_rows, item_rows

DIALECTS = {"ru": (";", ","), "en": (",", ".")}


def csv_cell(value, decimal: str = "."):
    """A value as CSV text: numbers without a trailing ".0" and with the region's decimal sign; text that
    starts like a formula is neutralized (numbers are not text, so a negative amount stays a number)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.10f}".rstrip("0").rstrip(".")
        return text.replace(".", decimal) if text not in ("-0", "") else "0"
    return csv_value(value)


def table_text(columns: list[str], rows: list[dict], lang: str = "en") -> str:
    delimiter, decimal = DIALECTS.get(lang, DIALECTS["en"])
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", delimiter=delimiter)
    writer.writeheader()
    writer.writerows({k: csv_cell(v, decimal) for k, v in row.items()} for row in rows)
    return out.getvalue()


def _write_table(path: Path, columns: list[str], rows: list[dict], lang: str = "en") -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        f.write(table_text(columns, rows, lang))


def write(results: list[ExtractionResult], path: Path, lang: str = "en") -> list[Path]:
    items_path = path.with_name(f"{path.stem}_items.csv")
    _write_table(path, *document_rows(results), lang=lang)
    _write_table(items_path, *item_rows(results), lang=lang)
    return [path, items_path]
