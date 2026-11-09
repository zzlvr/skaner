import json
from pathlib import Path

from docstruc.core.models import ExtractionResult


def to_dict(results: list[ExtractionResult]) -> list[dict]:
    rows = []
    for r in results:
        row = {
            "file": r.source,
            "doctype": r.doctype,
            "model": r.model,
            "needs_review": r.needs_review,
            "data": r.data,
            "issues": [i.model_dump() for i in r.issues],
        }
        if r.evidence:  # only present when the document was located; box = fractions of the page (x0, y0, x1, y1)
            row["evidence"] = {k: e.model_dump() for k, e in r.evidence.items()}
        rows.append(row)
    return rows


def from_dict(rows: list[dict]) -> list[ExtractionResult]:
    """Inverse of `to_dict`: load results saved as JSON."""
    return [
        ExtractionResult(
            source=r["file"], doctype=r["doctype"], model=r["model"], data=r["data"], issues=r["issues"],
            evidence=r.get("evidence", {}),
        )
        for r in rows
    ]


def write(results: list[ExtractionResult], path: Path) -> list[Path]:
    path.write_text(json.dumps(to_dict(results), ensure_ascii=False, indent=2), encoding="utf-8")
    return [path]
