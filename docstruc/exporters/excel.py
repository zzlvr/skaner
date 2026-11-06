"""Excel export: sheets "Documents", "Line items" and "Review". Values that need review are highlighted.

`lang="ru"` gives Russian sheet names, column names, statuses and review messages, and real dates.
"""

from datetime import date
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from docstruc.core.models import ExtractionResult
from docstruc.exporters import i18n
from docstruc.exporters.safe import neutralize_workbook
from docstruc.exporters.tabular import document_rows, issue_rows, item_rows

FLAG_FILL = PatternFill("solid", fgColor="FFF2CC")
HEADER_FONT = Font(bold=True)


def _localize(row: dict, lang: str) -> dict:
    if lang == "en":
        return row
    out = {}
    for key, val in row.items():
        if key in i18n.DATE_COLUMNS and isinstance(val, str):
            try:
                val = date.fromisoformat(val)
            except ValueError:
                pass
        out[key] = i18n.value(val, lang)
    return out


def _fill_sheet(ws, columns: list[str], rows: list[dict], lang: str = "en") -> None:
    ws.append([i18n.column(c, lang) for c in columns])
    for cell in ws[1]:
        cell.font = HEADER_FONT
    for row in rows:
        ws.append([row.get(c) for c in columns])
    ws.freeze_panes = "A2"
    for idx, col in enumerate(columns, start=1):
        if col in i18n.DATE_COLUMNS and lang != "en":
            for (cell,) in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
                cell.number_format = "DD.MM.YYYY"
        width = max([len(i18n.column(col, lang))] + [len(str(r.get(col) or "")) for r in rows[:200]])
        ws.column_dimensions[get_column_letter(idx)].width = min(max(width + 2, 8), 60)


def build_workbook(results: list[ExtractionResult], lang: str = "en") -> Workbook:
    wb = Workbook()

    docs = wb.active
    docs.title = i18n.sheet("Documents", lang)
    columns, rows = document_rows(results)
    _fill_sheet(docs, columns, [_localize(r, lang) for r in rows], lang)
    for row_idx, r in enumerate(results, start=2):
        flagged = r.review_fields
        for col_idx, col in enumerate(columns, start=1):
            if col in flagged or (col == "status" and r.needs_review):
                docs.cell(row=row_idx, column=col_idx).fill = FLAG_FILL

    items = wb.create_sheet(i18n.sheet("Line items", lang))
    item_columns, item_data = item_rows(results)
    _fill_sheet(items, item_columns, item_data, lang)
    flagged_lines = {
        (r.source, int(i.field.split(".")[1]) + 1)
        for r in results
        for i in r.issues
        if i.field.startswith("items.") and i.severity == "error"
    }
    for row_idx, row in enumerate(item_data, start=2):
        if (row["file"], row["line"]) in flagged_lines:
            for col_idx in range(1, len(item_columns) + 1):
                items.cell(row=row_idx, column=col_idx).fill = FLAG_FILL

    review = wb.create_sheet(i18n.sheet("Review", lang))
    review_columns, review_data = issue_rows(results)
    if lang != "en":
        review_data = [
            {**r, "field": i18n.field(r["field"], lang), "message": i18n.message(r["message"], lang),
             "severity": i18n.value(r["severity"], lang)}
            for r in review_data
        ]
    _fill_sheet(review, review_columns, review_data, lang)
    return neutralize_workbook(wb)


def write(results: list[ExtractionResult], target: Path | BinaryIO, lang: str = "en") -> list[Path]:
    build_workbook(results, lang).save(target)
    return [target] if isinstance(target, Path) else []


def to_bytes(results: list[ExtractionResult], lang: str = "en") -> bytes:
    buffer = BytesIO()
    write(results, buffer, lang)
    return buffer.getvalue()
