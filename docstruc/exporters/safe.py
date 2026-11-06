"""Text read from documents must never become a formula in a spreadsheet.

A document (or a file name) can contain text such as =HYPERLINK("http://…") or @SUM(…). Written as is,
Excel would run it as a formula: a clickable link to anywhere, or a value that changes. This is known
as CSV/Excel formula injection. Our files never contain formulas on purpose, so every formula cell is
turned back into plain text, and CSV values that start like a formula get a leading apostrophe.
"""

from openpyxl import Workbook

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def neutralize_workbook(wb: Workbook) -> Workbook:
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    return wb


def csv_value(value):
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value
