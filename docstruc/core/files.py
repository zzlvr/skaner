"""Loading input files into `Document` objects."""

from io import BytesIO
from pathlib import Path

from docstruc.core.models import Document

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
}

MAX_BYTES = 20 * 1024 * 1024


class UnsupportedFileError(ValueError):
    pass


def mime_type_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix not in MIME_TYPES:
        supported = ", ".join(sorted(MIME_TYPES))
        raise UnsupportedFileError(f"Unsupported file type '{suffix}'. Supported: {supported}")
    return MIME_TYPES[suffix]


# A small file can hold a huge image (e.g. 30 000 × 30 000 pixels) that takes gigabytes of memory to open.
MAX_IMAGE_PIXELS = 50_000_000
_PIL_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "MPO": "image/jpeg"}


def _checked_image(data: bytes, name: str) -> str:
    """The real image type, read from the content (the file name can lie). Rejects huge or broken images."""
    from PIL import Image

    try:
        # Only the decoders we accept are tried: a hostile file never reaches Pillow's other format parsers
        # (GIF, XBM, TIFF, EPS, MIC, ...), where most decoder bugs are found.
        with Image.open(BytesIO(data), formats=list(_PIL_FORMATS)) as image:
            width, height = image.size
            kind = image.format
    except Exception as exc:
        raise UnsupportedFileError(f"{name}: not a readable image") from exc
    if kind not in _PIL_FORMATS:
        raise UnsupportedFileError(f"{name}: unsupported image format {kind}")
    if width * height > MAX_IMAGE_PIXELS:
        raise UnsupportedFileError(f"{name}: image is too large ({width}×{height} pixels)")
    return _PIL_FORMATS[kind]


def from_bytes(data: bytes, name: str) -> Document:
    if not data:
        raise UnsupportedFileError(f"{name}: file is empty")
    if len(data) > MAX_BYTES:
        raise UnsupportedFileError(f"{name}: file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    mime = mime_type_for(name)
    if mime.startswith("image/") and mime != "image/heic":
        mime = _checked_image(data, name)
    elif mime == PDF and not data.startswith(b"%PDF"):
        raise UnsupportedFileError(f"{name}: not a PDF file")
    return Document(name=name, mime_type=mime, data=data)


PDF = "application/pdf"


def page_count(document: Document) -> int:
    """Pages in a file: every page of a PDF, 1 for an image. Each page is one call's worth of work."""
    if document.mime_type != PDF:
        return 1
    import pymupdf

    try:
        with pymupdf.open(stream=document.data, filetype="pdf") as pdf:
            return pdf.page_count
    except Exception as exc:
        raise UnsupportedFileError(f"{document.name}: the PDF cannot be read") from exc


def split_pages(document: Document) -> list[Document]:
    """A multi-page PDF as one-page PDFs (images and one-page PDFs are returned as is)."""
    if document.mime_type != PDF or page_count(document) == 1:
        return [document]
    import pymupdf

    pages = []
    with pymupdf.open(stream=document.data, filetype="pdf") as pdf:
        for n in range(pdf.page_count):
            one = pymupdf.open()
            one.insert_pdf(pdf, from_page=n, to_page=n)
            pages.append(Document(name=f"{document.name} p{n + 1}", mime_type=PDF, data=one.tobytes()))
            one.close()
    return pages


def from_path(path: str | Path) -> Document:
    path = Path(path)
    return from_bytes(path.read_bytes(), path.name)


def collect_paths(inputs: list[str | Path]) -> list[Path]:
    """Expand folders into the supported files they contain (non-recursive), keep files as is."""
    paths: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            paths.extend(sorted(f for f in p.iterdir() if f.suffix.lower() in MIME_TYPES))
        else:
            paths.append(p)
    return paths
