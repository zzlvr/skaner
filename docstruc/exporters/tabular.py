"""Flattening results into rows, shared by the Excel and CSV exporters."""

from typing import Any

from docstruc.core.models import ExtractionResult


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{name}."))
        elif isinstance(value, list):
            continue  # lists become their own rows, see item_rows
        else:
            flat[name] = value
    return flat


def document_rows(results: list[ExtractionResult]) -> tuple[list[str], list[dict[str, Any]]]:
    """One row per document: file name, status, then every scalar field (nested fields as `seller.name`)."""
    rows, columns = [], ["file", "status"]
    for r in results:
        flat = _flatten(r.data)
        for col in flat:
            if col not in columns:
                columns.append(col)
        rows.append({"file": r.source, "status": "needs review" if r.needs_review else "ok", **flat})
    return columns, rows


def item_rows(results: list[ExtractionResult]) -> tuple[list[str], list[dict[str, Any]]]:
    """One row per line item, linked to its document by file name."""
    rows, columns = [], ["file", "line"]
    for r in results:
        for i, item in enumerate(r.data.get("items") or [], start=1):
            flat = _flatten(item)
            for col in flat:
                if col not in columns:
                    columns.append(col)
            rows.append({"file": r.source, "line": i, **flat})
    return columns, rows


def issue_rows(results: list[ExtractionResult]) -> tuple[list[str], list[dict[str, Any]]]:
    columns = ["file", "field", "severity", "message"]
    rows = [
        {"file": r.source, "field": i.field, "severity": i.severity, "message": i.message}
        for r in results
        for i in r.issues
    ]
    return columns, rows
