"""Reading a document page by page into words with positions.

A PDF with a text layer is read exactly (PyMuPDF). Photos and scanned PDFs go through OCR when the optional
`rapidocr` package is installed (`pip install "docstruc[locate]"`); without it they give no words, and the
engine then simply has no positions for them.
"""

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO

from docstruc.core.models import Document

PDF = "application/pdf"
MIN_TEXT_WORDS = 5  # fewer words than this on a PDF page: treat it as a scan and use OCR
RENDER_MAX_SIDE = 2200  # pixels; enough for OCR, small enough to stay fast
IMAGE_MAX_SIDE = 2600


@dataclass(frozen=True)
class Word:
    text: str
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 as fractions of the page


@dataclass
class Page:
    part: int
    index: int
    image: bytes  # JPEG of the page, for reports
    words: list[Word] = field(default_factory=list)  # reading order
    ocr: bool = False  # words came from OCR (may contain misreads) rather than a text layer


def ocr_available() -> bool:
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

        # Cyrillic model: reads Russian, Kazakh (Cyrillic) and Latin text, and digits.
        _ENGINE = RapidOCR(
            params={
                "Rec.lang_type": LangRec.CYRILLIC,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
                "Global.log_level": "error",
            }
        )
    return _ENGINE


def _jpeg(image) -> bytes:
    out = BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=82)
    return out.getvalue()


def _ocr_words(image) -> list[Word]:
    import numpy as np

    width, height = image.size
    result = _engine()(np.asarray(image.convert("RGB"))[:, :, ::-1], return_word_box=True)
    if not result.txts:  # nothing readable: the engine returns a placeholder instead of an empty list
        return []
    words = []
    for line in result.word_results:  # lines come in reading order
        for text, _score, quad in line:
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            words.append(Word(text, (min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height)))
    return words


def _image_pages(part: int, document: Document, use_ocr: bool) -> list[Page]:
    from PIL import Image, ImageOps

    with Image.open(BytesIO(document.data)) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
    image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    words = _ocr_words(image) if use_ocr else []
    return [Page(part, 0, _jpeg(image), words, ocr=True)]


def _pdf_pages(part: int, document: Document, use_ocr: bool) -> list[Page]:
    import pymupdf
    from PIL import Image

    pages = []
    with pymupdf.open(stream=document.data, filetype="pdf") as pdf:
        for n, page in enumerate(pdf):
            rect = page.rect
            zoom = min(2.0, RENDER_MAX_SIDE / max(rect.width, rect.height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            raw = sorted(page.get_text("words"), key=lambda w: (w[5], w[6], w[7]))  # block, line, word
            if len(raw) >= MIN_TEXT_WORDS:
                words = [
                    Word(w[4], (w[0] / rect.width, w[1] / rect.height, w[2] / rect.width, w[3] / rect.height))
                    for w in raw
                ]
                pages.append(Page(part, n, _jpeg(image), words, ocr=False))
            else:
                pages.append(Page(part, n, _jpeg(image), _ocr_words(image) if use_ocr else [], ocr=True))
    return pages


@lru_cache(maxsize=4)
def _read(part: int, name: str, digest: str, mime: str, data: bytes, use_ocr: bool) -> tuple[Page, ...]:
    document = Document(name=name, mime_type=mime, data=data)
    if mime == PDF:
        return tuple(_pdf_pages(part, document, use_ocr))
    if mime.startswith("image/") and mime != "image/heic":
        return tuple(_image_pages(part, document, use_ocr))
    return ()


def read_pages(parts: list[Document]) -> list[Page]:
    """Every page of the given files, with word positions. Cached, so locating and reporting read a file once."""
    use_ocr = ocr_available()
    pages: list[Page] = []
    for n, document in enumerate(parts):
        digest = hashlib.sha1(document.data).hexdigest()
        pages.extend(_read(n, document.name, digest, document.mime_type, document.data, use_ocr))
    return pages
