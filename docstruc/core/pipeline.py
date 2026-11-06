"""Entry points of the engine.

- extract_all(file, doctype): every document found in one file (a photo may show several receipts).
- extract(files, doctype): exactly one document, possibly given as several ordered parts
  (e.g. a long receipt photographed in pieces).
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field, create_model

from docstruc.core import files
from docstruc.core.models import Document, ExtractionResult, Issue
from docstruc.core.normalize import normalize
from docstruc.doctypes import DocType, get_doctype
from docstruc.llm.base import LLMBackend

Source = str | Path | Document

MULTI_DOCUMENT_INSTRUCTIONS = """The file may contain several separate documents, for example two receipts photographed side by side.
Return every document as a separate entry in `documents`, in reading order (left to right, then top to bottom).
Never merge two documents into one entry, and never split one document into several.
If there is only one document, return a list with one entry. If there is no document at all, return an empty list."""


def _load(source: Source) -> Document:
    return source if isinstance(source, Document) else files.from_path(source)


def _backend(backend: LLMBackend | None) -> LLMBackend:
    if backend is not None:
        return backend
    from docstruc.llm import get_backend

    return get_backend()


_PAGE_SCHEMAS: dict[str, type[BaseModel]] = {}


def _page_schema(spec: DocType) -> type[BaseModel]:
    """A schema holding a list of documents of this type (built once per document type)."""
    if spec.name not in _PAGE_SCHEMAS:
        _PAGE_SCHEMAS[spec.name] = create_model(
            f"{spec.schema.__name__}Page",
            documents=(list[spec.schema], Field(description="Every separate document in the file, in reading order")),
        )
    return _PAGE_SCHEMAS[spec.name]


def _result(spec: DocType, parsed: BaseModel, source: str, backend: LLMBackend) -> ExtractionResult:
    normalize(parsed)
    return ExtractionResult(
        source=source,
        doctype=spec.name,
        model=backend.name,
        data=parsed.model_dump(mode="json"),
        issues=spec.validate(parsed),
    )


def extract(
    source: Source | Sequence[Source],
    doctype: str,
    backend: LLMBackend | None = None,
) -> ExtractionResult:
    """Extract exactly one document and validate it.

    `source` is a file path or a loaded `Document`. A list of them is treated as the
    ordered parts of a single document (e.g. a long receipt photographed in pieces).
    Nothing is stored: files are read, sent to the backend and the result is returned.
    """
    spec = get_doctype(doctype)
    sources = list(source) if isinstance(source, Sequence) and not isinstance(source, str) else [source]
    if not sources:
        raise ValueError("No input files given")
    parts = [_load(s) for s in sources]
    backend = _backend(backend)
    parsed = backend.extract(parts, spec.schema, spec.instructions)
    return _result(spec, parsed, " + ".join(p.name for p in parts), backend)


def extract_all(source: Source, doctype: str, backend: LLMBackend | None = None) -> list[ExtractionResult]:
    """Extract every document found in one file. Several documents get names like "photo.jpg #2"."""
    spec = get_doctype(doctype)
    document = _load(source)
    backend = _backend(backend)
    page = backend.extract([document], _page_schema(spec), f"{spec.instructions}\n{MULTI_DOCUMENT_INSTRUCTIONS}")
    found = page.documents
    if not found:
        return [
            ExtractionResult(
                source=document.name,
                doctype=spec.name,
                model=backend.name,
                data={},
                issues=[Issue(field="document", severity="error", message="No document found in the file")],
            )
        ]
    if len(found) == 1:
        return [_result(spec, found[0], document.name, backend)]
    return [_result(spec, doc, f"{document.name} #{n}", backend) for n, doc in enumerate(found, start=1)]
