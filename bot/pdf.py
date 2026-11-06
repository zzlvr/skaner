"""Photos and PDFs → one PDF file, page per photo, like a document scanner app. Runs locally: no AI, no cost.

Each photo goes through docstruc.scan: the sheet is found, straightened and cleaned (white paper,
sharp text, no shadows). PDFs sent by the user are appended as they are.
"""

import pymupdf

from docstruc.core.models import Document
from docstruc.scan import scan_image


def build_pdf(documents: list[Document]) -> tuple[bytes, int]:
    """Returns the PDF bytes and its page count."""
    pdf = pymupdf.open()
    for doc in documents:
        if doc.mime_type == "application/pdf":
            with pymupdf.open(stream=doc.data, filetype="pdf") as source:
                pdf.insert_pdf(source)
            continue
        jpeg, width, height, _ = scan_image(doc.data)
        # Page size in points matches the page proportions; the long side is A4 height (842 pt).
        scale = 842 / max(width, height)
        page = pdf.new_page(width=width * scale, height=height * scale)
        page.insert_image(page.rect, stream=jpeg)
    try:
        return pdf.tobytes(garbage=3, deflate=True), pdf.page_count
    finally:
        pdf.close()
