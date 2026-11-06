import json
from pathlib import Path

from docstruc.core.models import ExtractionResult


def to_dict(results: list[ExtractionResult]) -> list[dict]:
    return [
        {
            "file": r.source,
            "doctype": r.doctype,
            "model": r.model,
            "needs_review": r.needs_review,
            "data": r.data,
            "issues": [i.model_dump() for i in r.issues],
        }
        for r in results
    ]


def from_dict(rows: list[dict]) -> list[ExtractionResult]:
    """Inverse of `to_dict`: load results saved as JSON."""
    return [
        ExtractionResult(source=r["file"], doctype=r["doctype"], model=r["model"], data=r["data"], issues=r["issues"])
        for r in rows
    ]


def write(results: list[ExtractionResult], path: Path) -> list[Path]:
    path.write_text(json.dumps(to_dict(results), ensure_ascii=False, indent=2), encoding="utf-8")
    return [path]
