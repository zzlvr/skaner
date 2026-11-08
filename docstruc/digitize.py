"""Digitize: a photo of a document → a clean, typed PDF with real (selectable, searchable) text.

The model transcribes each page into a restricted HTML subset that keeps the structure
(headings, paragraphs, bold, lists, tables, alignment). The HTML is sanitized and laid out
on A4 pages with MuPDF's built-in fonts (Latin and Cyrillic).

Signatures, stamps, seals, logos and QR codes are never imitated: they become text
placeholders such as "[подпись]", so the result cannot pass for a forged original.
"""

import html
import re
from html.parser import HTMLParser
from io import BytesIO

import pymupdf
from pydantic import BaseModel, Field

from docstruc.core import files
from docstruc.core.models import Document
from docstruc.llm.base import LLMBackend


class DigitalPage(BaseModel):
    title: str | None = Field(None, description="Document title if printed, e.g. 'Договор поставки №12'")
    html: str = Field(description="The full page content as restricted HTML (see instructions)")


INSTRUCTIONS = """Transcribe this document page into clean HTML so it can be re-typeset as a digital document.

Rules:
- Copy every word, number and date exactly as printed, in the original language and format. Do not translate,
  summarize, correct or reformat anything (this overrides any rule about normalizing numbers or dates).
- Keep the structure: headings (<h1>, <h2>, <h3>), paragraphs (<p>), line breaks (<br>), bold (<b>), italic (<i>),
  underline (<u>), lists (<ul>/<ol>/<li>), tables (<table>, <tr>, <th>, <td>, with colspan/rowspan when cells are merged),
  horizontal rules (<hr>). Use style="text-align: center" or "text-align: right" on <p>, <h1>-<h3>, <td>, <th>
  when the text is centered or right-aligned. Use only these tags and attributes.
- Never reproduce signatures, stamps, seals, logos, photos, barcodes or QR codes. Put a short placeholder in square
  brackets in the document's language instead: [подпись], [печать], [логотип], [QR-код] / [signature], [stamp], [logo], [QR code].
- Handwritten text: transcribe it if readable. Unreadable fragments: [неразборчиво] / [illegible].
- Blank lines to fill in (e.g. "____________") are kept as underscores.
- Do not add anything that is not on the page."""

ALLOWED_TAGS = {
    "h1", "h2", "h3", "p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td", "hr", "div", "span",
}
VOID_TAGS = {"br", "hr"}
_ALIGN = re.compile(r"text-align\s*:\s*(left|center|right|justify)", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\[([^\[\]<>]{2,40})\]")


class _Sanitizer(HTMLParser):
    """Rebuild HTML keeping only the allowed tags and a few safe attributes; drops scripts, images, links, styles."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self._skip_depth += 1
            return
        if tag not in ALLOWED_TAGS:
            return
        kept = []
        for name, value in attrs:
            value = value or ""
            # isascii: "²".isdigit() is True but int("²") fails, which made the text PDF impossible to download.
            if name in ("colspan", "rowspan") and value.isascii() and value.isdigit() and 0 < int(value) <= 50:
                kept.append(f'{name}="{value}"')
            elif name == "style":
                align = _ALIGN.search(value)
                if align:
                    kept.append(f'style="text-align: {align[1].lower()}"')
        self.out.append(f"<{tag}{' ' if kept else ''}{' '.join(kept)}>")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self._skip_depth:
            self.out.append(html.escape(data, quote=False))


def sanitize_html(raw: str) -> str:
    parser = _Sanitizer()
    parser.feed(raw)
    parser.close()
    cleaned = "".join(parser.out)
    return _PLACEHOLDER.sub(lambda m: f'<span class="ph">[{m[1]}]</span>', cleaned)


# MuPDF's built-in fonts cover Latin and Cyrillic (with Noto fallbacks) and keep plain spaces
# in the text layer; embedding a system TTF made spaces come out as no-break spaces.
CSS = """
* {font-family: sans-serif;}
body {font-size: 10.5pt; line-height: 1.35;}
h1 {font-size: 15pt; font-weight: bold; margin: 0 0 8pt 0;}
h2 {font-size: 13pt; font-weight: bold; margin: 10pt 0 6pt 0;}
h3 {font-size: 11.5pt; font-weight: bold; margin: 8pt 0 4pt 0;}
p {margin: 0 0 5pt 0;}
table {border-collapse: collapse; width: 100%; margin: 4pt 0 8pt 0;}
th, td {border: 0.5pt solid #555; padding: 2pt 4pt; font-size: 9.5pt; vertical-align: top;}
th {font-weight: bold; background-color: #eeeeee;}
.ph {color: #777777;}
"""


# A table wider than the page is not shrunk by MuPDF: its right columns run off the page and are lost, also from
# the text layer (the sums of a 9-column waybill used to be cut off). Such a page is laid out on
# landscape A4, and if it is still too wide, with smaller table text.
NARROW_CSS = "body {font-size: 8.5pt;} th, td {font-size: 7pt; padding: 1pt 2pt;}"
LAYOUTS = (("a4", CSS), ("a4-l", CSS), ("a4-l", CSS + NARROW_CSS))
MARGINS = (50, 50, -50, -50)


def _fits(body: str, css: str, where: pymupdf.Rect) -> bool:
    story = pymupdf.Story(html=body, user_css=css)
    for _ in range(200):  # pages; a transcription is one source page, so this is never reached in practice
        more, filled = story.place(where)
        if filled[2] > where.x1 + 0.5:  # place() returns the filled area as a tuple (x0, y0, x1, y1)
            return False
        if not more:
            return True
        story.draw(None)  # place() moves on to the rest of the content only after a draw
    return True


def _layout(body: str) -> tuple[pymupdf.Rect, str]:
    for paper, css in LAYOUTS:
        mediabox = pymupdf.paper_rect(paper)
        if _fits(body, css, mediabox + MARGINS):
            return mediabox, css
    return mediabox, css  # the widest layout: still better than losing more columns


def render_pdf(pages: list[DigitalPage]) -> tuple[bytes, int]:
    """Lay out the transcribed pages on A4 (landscape for wide tables); each source page starts on a new PDF page."""
    buffer = BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    count = 0
    for page in pages:
        body = f"<body>{sanitize_html(page.html)}</body>"
        mediabox, css = _layout(body)
        where = mediabox + MARGINS
        story = pymupdf.Story(html=body, user_css=css)
        more = True
        while more:
            device = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
            count += 1
    writer.close()
    return buffer.getvalue(), count


def transcribe(document: Document, backend: LLMBackend) -> DigitalPage:
    return backend.extract([document], DigitalPage, INSTRUCTIONS)


def digitize(documents: list[Document], backend: LLMBackend) -> tuple[bytes, int]:
    """Photos/scans of one or more pages → one typed PDF. Returns the PDF bytes and its page count.

    A multi-page PDF is transcribed page by page: one long answer would be slow and could be cut off.
    """
    pages = [page for d in documents for page in files.split_pages(d)]
    if hasattr(backend, "cache_prefix"):
        backend.cache_prefix = len(pages) > 1
    return render_pdf([transcribe(p, backend) for p in pages])
