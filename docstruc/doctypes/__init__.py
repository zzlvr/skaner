"""Registry of document types. A new type is a module with a `DocType` registered here."""

from docstruc.doctypes.act import ACT
from docstruc.doctypes.base import DocType
from docstruc.doctypes.invoice import INVOICE
from docstruc.doctypes.receipt import RECEIPT
from docstruc.doctypes.waybill import WAYBILL

DOCTYPES: dict[str, DocType] = {d.name: d for d in (RECEIPT, INVOICE, ACT, WAYBILL)}


def get_doctype(name: str) -> DocType:
    try:
        return DOCTYPES[name]
    except KeyError:
        raise ValueError(f"Unknown document type '{name}'. Available: {', '.join(DOCTYPES)}") from None


__all__ = ["DocType", "DOCTYPES", "get_doctype"]
