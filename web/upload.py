"""Reading an upload: a streaming multipart parser that keeps files in memory only, and content checks.

Starlette's own form parser spools files over 1 MB to temporary files on disk and does not limit file
sizes, so uploads are parsed here: limits are enforced while the body is read, nothing touches the disk.
The CSRF token must be the first field of the form and is checked before any file byte is accepted.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from io import BytesIO
from typing import Callable

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from docstruc.core import files
from docstruc.core.models import Document

from .config import Settings
from .deps import CsrfFailed

FORM_OVERHEAD = 1024 * 1024  # multipart headers and small fields on top of the file bytes
MAX_FIELD = 1024
MAX_FIELDS = 16
MAX_HEADER = 4096
WEBP_MAX_PIXELS = 25_000_000  # WebP has no cheap reduced decode, so the check decodes it fully


class UploadRejected(Exception):
    """A problem with the upload the user can fix. `code` maps to a translated message."""

    def __init__(self, code: str, status: int = 400, index: int | None = None, name: str = ""):
        super().__init__(code)
        self.code, self.status, self.index, self.name = code, status, index, name


@dataclass
class RawFile:
    index: int  # 1-based position among the uploaded files
    name: str  # cleaned display name (no path, no control characters), never logged
    data: bytes = field(repr=False)


@dataclass
class ParsedUpload:
    fields: dict[str, str]
    files: list[RawFile]


def clean_name(raw: str, index: int) -> str:
    """Display name without directories, control characters or extension; at most 80 characters."""
    base = re.split(r"[\\/]", raw or "")[-1]
    base = "".join(c for c in unicodedata.normalize("NFC", base) if unicodedata.category(c)[0] not in "CZ" or c == " ")
    base = " ".join(base.split())
    if "." in base[1:]:
        base = base.rsplit(".", 1)[0]
    return base[:80].strip() or f"file {index}"


class _Parser:
    def __init__(self, settings: Settings, check_token: Callable[[str], bool]):
        self.s = settings
        self.check_token = check_token
        self.fields: dict[str, str] = {}
        self.files: list[RawFile] = []
        self.token_ok = False
        self.total = 0
        self.n_fields = 0
        self.parts = 0
        self._reset()

    def _reset(self) -> None:
        self.h_name = b""
        self.h_value = b""
        self.header_bytes = 0
        self.disposition = b""
        self.name = ""
        self.filename: str | None = None
        self.buf = bytearray()
        self.counted = False

    # python-multipart callbacks
    def on_part_begin(self) -> None:
        # Every part counts, also empty file parts (an unused file input): otherwise a body of hundreds of thousands
        # of empty parts passes every other limit and costs many seconds of CPU per request.
        self.parts += 1
        if self.parts > self.s.max_files + MAX_FIELDS:
            raise UploadRejected("bad_form")
        self._reset()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_bytes(end - start)
        self.h_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_bytes(end - start)
        self.h_value += data[start:end]

    def _header_bytes(self, n: int) -> None:
        self.header_bytes += n
        if self.header_bytes > MAX_HEADER:
            raise UploadRejected("bad_form")

    def on_header_end(self) -> None:
        if self.h_name.lower() == b"content-disposition":
            self.disposition = self.h_value
        self.h_name, self.h_value = b"", b""

    def on_headers_finished(self) -> None:
        _, options = parse_options_header(self.disposition)
        if b"name" not in options:
            raise UploadRejected("bad_form")
        self.name = options[b"name"].decode("utf-8", "replace")
        if b"filename" in options:
            if not self.token_ok:  # the token comes first: no file byte before the request is known to be ours
                raise CsrfFailed()
            self.filename = options[b"filename"].decode("utf-8", "replace")
        else:
            self.n_fields += 1
            if self.n_fields > MAX_FIELDS:
                raise UploadRejected("bad_form")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        n = end - start
        if self.filename is None:
            if len(self.buf) + n > MAX_FIELD:
                raise UploadRejected("bad_form")
            self.buf += data[start:end]
            return
        if n and not self.counted:
            self.counted = True
            if len(self.files) + 1 > self.s.max_files:
                raise UploadRejected("too_many_files", 413)
        index = len(self.files) + 1
        if len(self.buf) + n > self.s.max_file_bytes:
            raise UploadRejected("file_too_large", 413, index, clean_name(self.filename, index))
        self.total += n
        if self.total > self.s.max_batch_bytes:
            raise UploadRejected("batch_too_large", 413)
        self.buf += data[start:end]

    def on_part_end(self) -> None:
        if self.filename is None:
            value = self.buf.decode("utf-8", "replace")
            if self.name == "csrf_token" and not self.token_ok:
                if not self.check_token(value):
                    raise CsrfFailed()
                self.token_ok = True
            elif not self.token_ok:
                raise CsrfFailed()
            self.fields.setdefault(self.name, value)
        elif self.buf and self.name == "files":  # an input left empty sends a part with no bytes: skipped
            index = len(self.files) + 1
            self.files.append(RawFile(index, clean_name(self.filename, index), bytes(self.buf)))
        self.buf = bytearray()

    def on_end(self) -> None:
        pass

    def callbacks(self) -> dict:
        return {name: getattr(self, name) for name in (
            "on_part_begin", "on_part_data", "on_part_end", "on_header_field", "on_header_value",
            "on_header_end", "on_headers_finished", "on_end")}


def boundary_of(content_type: str) -> bytes | None:
    ctype, params = parse_options_header(content_type or "")
    if ctype != b"multipart/form-data":
        return None
    boundary = params.get(b"boundary")
    return boundary if boundary and len(boundary) <= 200 else None


async def parse_upload(stream, boundary: bytes, settings: Settings, check_token: Callable[[str], bool]) -> ParsedUpload:
    """Feed the request stream through the parser. On a user error the rest of the body is read and discarded
    (its size is already bounded by Content-Length), so the browser receives our error page."""
    p = _Parser(settings, check_token)
    parser = MultipartParser(boundary, p.callbacks())
    error: UploadRejected | None = None
    async for chunk in stream:
        if error is not None or not chunk:
            continue
        try:
            parser.write(chunk)
        except UploadRejected as exc:
            error = exc
            p.files.clear()
            p.buf = bytearray()
        except FormParserError:
            error = UploadRejected("bad_form")
            p.files.clear()
    if error is not None:
        raise error
    try:
        parser.finalize()
    except FormParserError:
        raise UploadRejected("bad_form") from None
    if not p.token_ok:
        raise CsrfFailed()
    return ParsedUpload(p.fields, p.files)


# ---------- content checks ----------

PNG_SIG = b"\x89PNG\r\n\x1a\n"
PIL_FORMATS = {"png": ["PNG"], "jpg": ["JPEG", "MPO"], "webp": ["WEBP"]}
HEIF_BRANDS ={b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}


def sniff(data: bytes) -> str | None:
    """File type from the first bytes. The name and the browser's Content-Type are never trusted."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(PNG_SIG):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return "heic"
    return None


def _check_image(data: bytes, kind: str) -> None:
    """Size from the header, then a cheap full read: catches truncated and broken files and pixel bombs."""
    from PIL import Image

    try:
        with Image.open(BytesIO(data), formats=PIL_FORMATS[kind]) as im:  # only the decoder of the sniffed type
            width, height = im.size
            if width * height > files.MAX_IMAGE_PIXELS or (kind == "webp" and width * height > WEBP_MAX_PIXELS):
                raise UploadRejected("file_pixels")
            if kind == "png":
                im.verify()  # every chunk checksum up to the end marker; no pixel decoding
            else:
                if kind == "jpg":
                    im.draft("RGB", (max(1, width // 8), max(1, height // 8)))  # decode at 1/8 scale
                im.load()
    except UploadRejected:
        raise
    except Image.DecompressionBombError:
        raise UploadRejected("file_pixels") from None
    except Exception:
        raise UploadRejected("file_unreadable") from None


def validate(raw: RawFile, settings: Settings) -> tuple[Document, int]:
    """A checked engine Document and its page count, or UploadRejected (with the file's index and name)."""
    try:
        if not raw.data:
            raise UploadRejected("file_empty")
        if len(raw.data) > min(settings.max_file_bytes, files.MAX_BYTES):
            raise UploadRejected("file_too_large", 413)
        kind = sniff(raw.data)
        if kind == "heic":
            raise UploadRejected("file_heic")  # the recognition service does not take HEIC
        if kind is None:
            raise UploadRejected("file_type")
        if kind != "pdf":
            _check_image(raw.data, kind)
        try:
            doc = files.from_bytes(raw.data, f"{raw.name}.{kind}")  # the engine's own checks, by content
            pages = files.page_count(doc)
        except files.UnsupportedFileError:
            raise UploadRejected("pdf_unreadable" if kind == "pdf" else "file_unreadable") from None
        if pages < 1:
            raise UploadRejected("pdf_unreadable")
        if pages > settings.max_pdf_pages:
            raise UploadRejected("pdf_pages")
        return doc, pages
    except UploadRejected as exc:
        exc.index, exc.name = raw.index, raw.name
        raise
