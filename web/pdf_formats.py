"""The two PDF formats of the website (the four table formats are in cabinet.py).

- pdf_scan ("PDF-скан"): each photo is straightened and cleaned by docstruc.scan and put on a PDF page; a PDF
  that was uploaded is appended as it is. No recognition, no AI, free. Same idea as the bot's "Скан в PDF"
  (bot/pdf.py), with two extra guards for the website: a very large photo is reduced before OpenCV works on it
  (bounded memory with 2 workers), and the result is scrubbed (no links, scripts, attachments, form fields).
- pdf_text ("PDF с текстом"): every page is transcribed by the recognition backend (docstruc.digitize) and the
  recognised pages are stored encrypted; the PDF is laid out again on each download (digitize.render_pdf, which
  sanitizes the HTML: no images, no links, so a page cannot pull in a file or a URL).
"""

from io import BytesIO

import pymupdf

from docstruc.core.files import PDF
from docstruc.core.models import Document
from docstruc.digitize import DigitalPage, render_pdf
from docstruc.scan import scan_image

SCAN, TEXT = "pdf_scan", "pdf_text"
KINDS = {SCAN: "scan", TEXT: "text"}
PRE_MAX_SIDE = 4000  # px: scan output is at most 2400 px anyway; bigger photos are reduced before OpenCV
A4_LONG_SIDE = 842  # pt

# Text of a page that could not be recognised, in the language of exported files (decision 30: ru or en).
FAILED_PAGE = {"ru": "[страница {n} не распознана]", "en": "[page {n} not recognised]"}


def _bounded(data: bytes) -> bytes:
    """The photo as is, or (when a side is above PRE_MAX_SIDE) a reduced JPEG copy, already upright."""
    from PIL import Image, ImageOps

    with Image.open(BytesIO(data)) as im:
        if max(im.size) <= PRE_MAX_SIDE:
            return data
        if im.format == "JPEG":
            im.draft("RGB", (PRE_MAX_SIDE, PRE_MAX_SIDE))  # cheap reduced decode, still at least this size
        upright = ImageOps.exif_transpose(im).convert("RGB")
    upright.thumbnail((PRE_MAX_SIDE, PRE_MAX_SIDE), Image.LANCZOS)
    out = BytesIO()
    upright.save(out, "JPEG", quality=92)  # no EXIF: the orientation is already applied
    upright.close()
    return out.getvalue()


def build_scan_pdf(documents: list[Document], progress=None) -> tuple[bytes, int]:
    """One PDF with all pages in upload order. Returns the PDF bytes and its page count."""
    pdf = pymupdf.open()
    try:
        for n, doc in enumerate(documents, start=1):
            if doc.mime_type == PDF:
                with pymupdf.open(stream=doc.data, filetype="pdf") as source:
                    pdf.insert_pdf(source, links=False, annots=False, widgets=False)
            else:
                jpeg, width, height, _ = scan_image(_bounded(doc.data))
                scale = A4_LONG_SIDE / max(width, height)
                page = pdf.new_page(width=width * scale, height=height * scale)
                page.insert_image(page.rect, stream=jpeg)
            if progress is not None:
                progress(n)
        # Keep what is visible (including a scanner's hidden text layer); drop everything active or attached.
        pdf.scrub(hidden_text=False, redactions=False, clean_pages=False)
        return pdf.tobytes(garbage=3, deflate=True), pdf.page_count
    finally:
        pdf.close()


def text_pdf(pages: list[dict], files_lang: str) -> bytes:
    """Stored recognised pages ({"title", "html"} or {"failed": true}) -> the PDF, laid out on A4."""
    layout = []
    for n, page in enumerate(pages, start=1):
        if page.get("failed"):
            layout.append(DigitalPage(html="<p>" + FAILED_PAGE[files_lang].format(n=n) + "</p>"))
        else:
            layout.append(DigitalPage(title=page.get("title"), html=page.get("html") or ""))
    content, _ = render_pdf(layout)
    return content
