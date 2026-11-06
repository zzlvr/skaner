"""Output formats. Each exporter writes a batch of results to a file or binary stream.

A new format is a module with a `write(results, target)` function registered in FORMATS.
"""

from pathlib import Path

from docstruc.exporters import csv_export, excel, json_export, onec

FORMATS = {
    "json": json_export.write,
    "xlsx": excel.write,
    "csv": csv_export.write,
    "1c": onec.write,  # Excel prepared for import into 1C
}
# Format chosen from the file extension when none is given explicitly.
EXPORTERS = {
    ".json": json_export.write,
    ".xlsx": excel.write,
    ".csv": csv_export.write,
}
FORMAT_SUFFIX = {"json": ".json", "xlsx": ".xlsx", "csv": ".csv", "1c": ".xlsx"}


def export(results, path: str | Path, fmt: str | None = None) -> list[Path]:
    """Write `results` to `path`. The format is `fmt`, or else chosen by file extension."""
    path = Path(path)
    if fmt is not None:
        if fmt not in FORMATS:
            raise ValueError(f"Unknown format '{fmt}'. Use one of: {', '.join(FORMATS)}")
        if path.suffix.lower() != FORMAT_SUFFIX[fmt]:
            raise ValueError(f"Format '{fmt}' writes a {FORMAT_SUFFIX[fmt]} file, got '{path.name}'")
        return FORMATS[fmt](results, path)
    try:
        writer = EXPORTERS[path.suffix.lower()]
    except KeyError:
        raise ValueError(f"Unsupported output format '{path.suffix}'. Use one of: {', '.join(EXPORTERS)}") from None
    return writer(results, path)
