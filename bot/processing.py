"""Turn a batch of received files into one output file. Independent of Telegram, so it is easy to test."""

from dataclasses import dataclass

from bot.billing import Job, cost_units
from docstruc.core.files import page_count
from docstruc.core.models import Document, ExtractionResult
from docstruc.core.pipeline import extract, extract_all
from docstruc.exporters import excel, onec
from docstruc.llm.base import LLMBackend


@dataclass
class BatchOutput:
    content: bytes
    filename: str
    documents: int
    needs_review: int
    failed: int  # files that could not be processed
    not_found: int  # files where no document was found (billed: the work was done)
    jobs: list[Job]  # what to bill, see bot/billing.py


def process_batch(
    files: list[Document],
    doctype: str,
    fmt: str,
    backend: LLMBackend,
    one_document: bool = False,
    lang: str = "en",
) -> BatchOutput:
    """Extract every file (or all files as one document) and build an Excel or 1C workbook."""
    jobs = [files] if one_document else [[f] for f in files]
    if hasattr(backend, "cache_prefix"):
        backend.cache_prefix = len(jobs) > 1
    results: list[ExtractionResult] = []
    jobs_billed: list[Job] = []
    failed = 0
    for job in jobs:
        label = " + ".join(f.name for f in job)
        pages = sum(page_count(f) for f in job)
        spent_before = getattr(backend, "cost_usd", None) or 0.0
        try:
            if one_document:
                found = [extract(job, doctype, backend=backend)]
            else:
                found = extract_all(job[0], doctype, backend=backend)
            results.extend(found)
            ok, units = True, max(pages, len(found))
        except Exception as exc:  # one bad file must not lose the whole batch
            results.append(ExtractionResult.failed(label, doctype, type(exc).__name__))
            failed += 1
            ok, units = False, 0
        spent = (getattr(backend, "cost_usd", None) or 0.0) - spent_before
        jobs_billed.append(Job(units=units, cost_units=cost_units(spent), ok=ok))

    if fmt == "1c":
        content, filename = onec.to_bytes(results), "документы_1С.xlsx" if lang == "ru" else "documents_1c.xlsx"
    else:
        content = excel.to_bytes(results, lang=lang)
        filename = "документы.xlsx" if lang == "ru" else "documents.xlsx"
    not_found = sum(1 for r in results if not r.data) - failed
    return BatchOutput(
        content=content,
        filename=filename,
        documents=len(results),
        needs_review=sum(r.needs_review for r in results),
        failed=failed,
        not_found=not_found,
        jobs=jobs_billed,
    )
