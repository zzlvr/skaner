"""Background recognition of accepted batches.

- Files stay in memory (a list of Documents) from upload until the batch ends, then the list is cleared.
- MemoryBudget caps the bytes held by batches being read, queued or processed (in total and per user).
- A bounded thread pool (WEB_WORKERS, default 2) processes batches; the rest wait as "queued".
- On start-up, batches left queued/running by a previous process are marked failed ("restart") and not charged.
  This assumes ONE web process (uvicorn without --workers): a second process would fail the first one's jobs.
- Logs carry counts and exception class names only: never file names, document text or values.
- A batch can run with the user's own provider key (ByokKey, "bring your own key"): the key is passed in memory
  from the upload to the job, never stored, logged or shown; nothing is charged and the owner gets no service alerts.
- Three kinds of batch (by Batch.fmt): tables (extraction), pdf_scan (local, free, no model) and pdf_text
  (recognition of the whole text, charged by the same rules as extraction). All use the same pool and budget.
"""

import logging
import threading
from types import SimpleNamespace
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from .billing_rules import Job, cost_units
from docstruc.core.files import page_count, split_pages
from docstruc.core.models import Document, ExtractionResult
from docstruc.core.pipeline import extract, extract_all
from docstruc.validation.batch import find_duplicates
from docstruc.digitize import transcribe
from docstruc.exporters import json_export

from . import balance as bal
from . import crypto
from .config import Settings
from .models import Batch, LedgerEntry
from .pdf_formats import SCAN, TEXT, build_scan_pdf

log = logging.getLogger("skaner.web.jobs")

ACTIVE = ("queued", "running")

# Exception class -> short safe label stored in the database (never the exception message).
ERROR_CODES = {
    "ExtractionError": "service", "APIConnectionError": "service", "APIStatusError": "service",
    "APITimeoutError": "service", "RateLimitError": "service", "InternalServerError": "service",
    "TimeoutError": "service", "ConnectionError": "service",
    "ValidationError": "format", "UnsupportedFileError": "file",
    "UnidentifiedImageError": "file", "DecompressionBombError": "file", "FileDataError": "file",
    "AuthenticationError": "key", "PermissionDeniedError": "key", "QuotaExceededError": "quota",
}


def error_code(exc: BaseException) -> str:
    """Only the class name and a few well-known phrases are looked at; the message itself is never kept."""
    name = type(exc).__name__
    text = str(exc).lower() if name in ("ExtractionError", "BadRequestError") else ""
    if "api key not valid" in text or "api error 401" in text or "api error 403" in text:
        return "key"
    if "credit balance" in text:
        return "quota"
    return ERROR_CODES.get(name, "internal")


# What a bring-your-own-key batch runs on: provider name on the site -> (backend, model). The key is the user's, so
# the cost is theirs: the cheaper tested model, not the premium one used for paid business batches.
BYOK_PROVIDERS = {"anthropic": ("claude", "claude-sonnet-5-5"), "gemini": ("gemini", None)}


class ByokKey:
    """The user's own provider key, kept in memory for one batch. Never stored, logged or shown (repr hides it)."""

    __slots__ = ("provider", "_key")

    def __init__(self, provider: str, key: str):
        self.provider, self._key = provider, key

    def __repr__(self) -> str:
        return f"ByokKey(provider={self.provider!r})"

    __str__ = __repr__

    def make_backend(self):
        from docstruc.llm import get_backend

        name, model = BYOK_PROVIDERS[self.provider]
        return get_backend(name, model=model, api_key=self._key)

    def wipe(self) -> None:
        self._key = ""


class Ticket:
    def __init__(self, budget: "MemoryBudget", user_id: int, size: int):
        self._budget, self.user_id, self.size, self._done = budget, user_id, size, False

    def release(self) -> None:
        self._budget._release(self)


class MemoryBudget:
    """Bytes of uploads held in memory. Reserved from Content-Length before the body is read."""

    def __init__(self, total: int, per_user: int):
        self.total, self.per_user = total, per_user
        self._lock = threading.Lock()
        self._used = 0
        self._by_user: dict[int, int] = {}

    def reserve(self, user_id: int, size: int) -> Ticket | None:
        with self._lock:
            mine = self._by_user.get(user_id, 0)
            if self._used + size > self.total or mine + size > self.per_user:
                return None
            self._used += size
            self._by_user[user_id] = mine + size
            return Ticket(self, user_id, size)

    def _release(self, ticket: Ticket) -> None:
        with self._lock:
            if ticket._done:
                return
            ticket._done = True
            self._used -= ticket.size
            left = self._by_user.get(ticket.user_id, 0) - ticket.size
            if left > 0:
                self._by_user[ticket.user_id] = left
            else:
                self._by_user.pop(ticket.user_id, None)

    @property
    def used(self) -> int:
        return self._used


class UploadGate:
    """Bounds the memory that checking uploads can take, which MemoryBudget does not see: a 1 KB WebP can declare
    25 megapixels and costs ~100 MB while it is decoded (web/upload._check_image). Without this gate one account
    could send many uploads at once (each one is refused as "active" only after its files were checked) and many
    accounts many more, and the decoding alone would run the server out of memory.
    - One upload being received and checked per user at a time (a second one in parallel is refused at once).
    - At most `uploads` uploads in that stage on the whole site (each one holds a worker thread and a database
      connection while it waits): more are refused at once as busy, so they cannot starve the rest of the site.
    - At most `decoders` files are checked at the same moment on the whole site; others wait up to `wait` seconds.
    """

    def __init__(self, decoders: int, uploads: int | None = None, wait: float = 20.0):
        self._lock = threading.Lock()
        self._users: set[int] = set()
        self._decode = threading.BoundedSemaphore(max(1, decoders))
        self.max_uploads = uploads if uploads is not None else max(4, 4 * decoders)
        self.wait = wait

    def enter(self, user_id: int) -> str | None:
        """None when the upload may go on, else the refusal code: active (this user) or busy (the site)."""
        with self._lock:
            if user_id in self._users:
                return "active"
            if len(self._users) >= self.max_uploads:
                return "busy"
            self._users.add(user_id)
            return None

    def leave(self, user_id: int) -> None:
        with self._lock:
            self._users.discard(user_id)

    def acquire_decoder(self) -> bool:
        return self._decode.acquire(timeout=self.wait)

    def release_decoder(self) -> None:
        self._decode.release()


class InlineExecutor:
    """Runs a job immediately in the calling thread (tests: deterministic, no sleeping)."""

    def submit(self, fn, *args):
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        pass


@dataclass
class Context:
    settings: Settings
    sessionmaker: sessionmaker
    clock: Callable[[], datetime]
    backend_factory: Callable[[], object]
    alerter: object | None = None  # web/alerts.Alerter: owner alerts for service problems (None in most tests)

    def alert_recognition(self, exc: BaseException, byok: "ByokKey | None" = None) -> None:
        if byok is None and self.alerter is not None:  # a failure of the user's own key is not the owner's problem
            self.alerter.recognition_failed(exc)

    def make_backend(self, byok: "ByokKey | None" = None):
        return byok.make_backend() if byok is not None else self.backend_factory()


def mark_stale(ctx: Context) -> int:
    """Start-up: jobs of a dead process cannot finish. Failed, not charged (their reservation stays counted)."""
    now = ctx.clock()
    with ctx.sessionmaker() as db:
        n = db.execute(
            update(Batch).where(Batch.status.in_(ACTIVE))
            .values(status="failed", error_code="restart", finished_at=now)
        ).rowcount
        db.commit()
    if n:
        log.warning("batches interrupted by a restart: %d", n)
    return n


def payer(batch: Batch) -> int:
    """Whose page balance pays: fixed at upload (the author, or the company owner for a company batch). NULL only
    when that account was deleted meanwhile: 0 then matches no user, so the charge is recorded without a user
    (bal.charge) and never falls on the author."""
    return batch.wallet_user_id if batch.wallet_user_id is not None else 0


def run_batch(ctx: Context, batch_id: int, documents: list[Document], ticket: Ticket,
              byok: ByokKey | None = None) -> None:
    try:
        _run(ctx, batch_id, documents, byok)
    except Exception as exc:  # noqa: BLE001  (a bug here must not leave the batch "running" forever)
        log.error("batch crashed: %s", type(exc).__name__)
        ctx.alert_recognition(exc, byok)
        try:
            with ctx.sessionmaker() as db:
                db.execute(update(Batch).where(Batch.id == batch_id, Batch.status.in_(ACTIVE))
                           .values(status="failed", error_code="internal", finished_at=ctx.clock()))
                db.commit()
        except Exception as exc2:  # noqa: BLE001
            log.error("could not mark batch failed: %s", type(exc2).__name__)
    finally:
        documents.clear()  # drop the file bytes
        if byok is not None:
            byok.wipe()
        ticket.release()


def _settle(db, batch: Batch, billed: list[Job], now: datetime):
    """Charge the batch from the wallet and fix the day's reservation; a bring-your-own-key batch charges nothing
    (and reserved nothing)."""
    if batch.byok:
        return SimpleNamespace(charged=0, overrun=0, work_units=sum(j.units for j in billed))
    bill = bal.charge(db, payer(batch), batch.public_id, billed, batch.created_at, now)
    bal.adjust_daily(db, bal.day_key(batch.created_at), bill.work_units - batch.pages_estimated,
                     trial=batch.trial_funded)
    return bill


def _run(ctx: Context, batch_id: int, documents: list[Document], byok: ByokKey | None = None) -> None:
    with ctx.sessionmaker() as db:
        started = db.execute(
            update(Batch).where(Batch.id == batch_id, Batch.status == "queued")
            .values(status="running", started_at=ctx.clock())
        ).rowcount
        db.commit()
        if started != 1:
            return
        batch = db.get(Batch, batch_id)
        if batch.fmt == SCAN:
            return _run_scan(ctx, db, batch, documents)
        if batch.fmt == TEXT:
            return _run_text(ctx, db, batch, documents, byok)
        doctype, one_document = batch.doctype, batch.one_document

        jobs = [list(documents)] if one_document else [[d] for d in documents]
        results: list[ExtractionResult] = []
        billed: list[Job] = []
        first_error: str | None = None
        try:
            backend = ctx.make_backend(byok)
        except Exception as exc:  # noqa: BLE001  (no API key, bad config: nothing was spent)
            backend, first_error = None, "service"
            log.error("recognition backend unavailable: %s", type(exc).__name__)
            ctx.alert_recognition(exc, byok)
        if backend is not None:
            if hasattr(backend, "cache_prefix"):
                backend.cache_prefix = len(jobs) > 1  # same as bot/processing.py: cache instructions in batches
            for n, job in enumerate(jobs, start=1):
                # Same rules as bot/processing.process_batch (which only returns a finished file, not results).
                pages = sum(page_count(f) for f in job)
                spent_before = getattr(backend, "cost_usd", None) or 0.0
                try:
                    found = [extract(job, doctype, backend=backend)] if one_document \
                        else extract_all(job[0], doctype, backend=backend)
                    results.extend(found)
                    ok, units = True, max(pages, len(found))
                except Exception as exc:  # noqa: BLE001  (one bad file must not lose the batch)
                    code = error_code(exc)
                    first_error = first_error or code
                    results.append(ExtractionResult.failed(" + ".join(f.name for f in job), doctype, code))
                    ok, units = False, 0
                    log.warning("file failed: %s", type(exc).__name__)
                    ctx.alert_recognition(exc, byok)
                spent = (getattr(backend, "cost_usd", None) or 0.0) - spent_before
                billed.append(Job(units=units, cost_units=cost_units(spent), ok=ok))
                db.execute(update(Batch).where(Batch.id == batch_id).values(files_done=len(job) if one_document else n))
                db.commit()
        documents.clear()

        now = ctx.clock()
        all_failed = not billed or not any(j.ok for j in billed)
        bill = _settle(db, batch, billed, now)
        find_duplicates(results)  # the same document twice in one upload
        found_docs = [r for r in results if r.data]
        failed_jobs = sum(1 for j in billed if not j.ok)
        values = dict(
            status="failed" if all_failed else "done", finished_at=now, files_done=batch.files_count,
            pages_charged=bill.charged - bill.overrun, work_units=bill.work_units,
            docs_found=len(found_docs), needs_review=sum(r.needs_review for r in found_docs),
            failed_files=failed_jobs if billed else len(jobs),
            not_found=len(results) - len(found_docs) - failed_jobs,  # "no document in this file" placeholders
            error_code=first_error if all_failed else None,
        )
        if not all_failed:
            rows = json_export.to_dict(results)
            for row in rows:
                row["model"] = ""  # the provider is not part of the user's data
            values["results_enc"] = crypto.encrypt(ctx.settings, batch.public_id, {"v": 1, "results": rows})
        db.execute(update(Batch).where(Batch.id == batch_id).values(**values))
        db.commit()  # charge, ledger row, daily usage and the batch's final state together
        log.info("batch finished: status=%s files=%d docs=%d review=%d charged=%d overrun=%d work=%d",
                 values["status"], batch.files_count, values["docs_found"], values["needs_review"],
                 bill.charged, bill.overrun, bill.work_units)


def _progress(db, batch_id: int, done: int) -> None:
    db.execute(update(Batch).where(Batch.id == batch_id).values(files_done=done))
    db.commit()


def _run_scan(ctx: Context, db, batch: Batch, documents: list[Document]) -> None:
    """Free scan: no backend is created, nothing is charged. The PDF is kept encrypted for a short time only."""
    error, content, pages = None, b"", 0
    try:
        content, pages = build_scan_pdf(documents, lambda n: _progress(db, batch.id, n))
    except Exception as exc:  # noqa: BLE001
        error = error_code(exc)
        log.warning("scan failed: %s", type(exc).__name__)
    documents.clear()
    now = ctx.clock()
    db.add(LedgerEntry(user_id=batch.user_id, batch_ref=batch.public_id, reason="scan_free", trial_delta=0,
                       paid_delta=0, charged=0, overrun=0, work_units=0, free_failures=0, created_at=now))
    values = dict(finished_at=now, files_done=batch.files_count, pages_charged=0, work_units=0, needs_review=0,
                  not_found=0)
    if error is not None:
        # The user's allowance comes back; the site's CPU counter keeps the work that was done.
        bal.adjust_scan(db, batch.user_id, bal.day_key(batch.created_at), -batch.pages_estimated, site_too=False)
        values.update(status="failed", error_code=error, docs_found=0, failed_files=batch.files_count)
    else:
        values.update(status="done", error_code=None, docs_found=pages, failed_files=0,
                      file_enc=crypto.encrypt_file(ctx.settings, batch.public_id, content),
                      file_expires_at=now + timedelta(minutes=ctx.settings.scan_file_minutes))
    content = b""
    db.execute(update(Batch).where(Batch.id == batch.id).values(**values))
    db.commit()
    log.info("scan finished: status=%s files=%d pages=%d", values["status"], batch.files_count, pages)


def _run_text(ctx: Context, db, batch: Batch, documents: list[Document], byok: ByokKey | None = None) -> None:
    """Whole-text recognition. One job per page (1 page = 1 unit, cost floor, failed-job rules of bot/billing).
    A page that fails becomes a marked placeholder page in the PDF and is not charged by the page rule."""
    pages_out: list[dict] = []
    billed: list[Job] = []
    first_error: str | None = None
    try:
        backend = ctx.make_backend(byok)
    except Exception as exc:  # noqa: BLE001  (no API key, bad config: nothing was spent)
        backend, first_error = None, "service"
        log.error("recognition backend unavailable: %s", type(exc).__name__)
        ctx.alert_recognition(exc, byok)
    if backend is not None:
        if hasattr(backend, "cache_prefix"):
            backend.cache_prefix = batch.pages_estimated > 1
        for n, doc in enumerate(documents, start=1):
            try:
                parts = split_pages(doc)
            except Exception as exc:  # noqa: BLE001
                first_error = first_error or error_code(exc)
                pages_out.append({"failed": True})
                billed.append(Job(units=0, cost_units=0, ok=False))
                log.warning("file failed: %s", type(exc).__name__)
                parts = []
            for part in parts:
                spent_before = getattr(backend, "cost_usd", None) or 0.0
                try:
                    page = transcribe(part, backend)
                    pages_out.append({"title": page.title, "html": page.html})
                    ok, units = True, 1
                except Exception as exc:  # noqa: BLE001  (one bad page must not lose the batch)
                    first_error = first_error or error_code(exc)
                    pages_out.append({"failed": True})
                    ok, units = False, 0
                    log.warning("page failed: %s", type(exc).__name__)
                    ctx.alert_recognition(exc, byok)
                spent = (getattr(backend, "cost_usd", None) or 0.0) - spent_before
                billed.append(Job(units=units, cost_units=cost_units(spent), ok=ok))
            parts.clear()
            _progress(db, batch.id, n)
    documents.clear()

    now = ctx.clock()
    all_failed = not billed or not any(j.ok for j in billed)
    bill = _settle(db, batch, billed, now)
    ok_pages = sum(1 for j in billed if j.ok)
    values = dict(
        status="failed" if all_failed else "done", finished_at=now, files_done=batch.files_count,
        pages_charged=bill.charged - bill.overrun, work_units=bill.work_units, docs_found=ok_pages, needs_review=0,
        failed_files=len(billed) - ok_pages if billed else batch.pages_estimated, not_found=0,
        error_code=first_error if all_failed else None,
    )
    if not all_failed:
        values["results_enc"] = crypto.encrypt(ctx.settings, batch.public_id, {"v": 1, "kind": "text", "pages": pages_out})
    db.execute(update(Batch).where(Batch.id == batch.id).values(**values))
    db.commit()  # charge, ledger row, daily usage and the batch's final state together
    log.info("text pdf finished: status=%s files=%d pages=%d failed=%d charged=%d overrun=%d work=%d",
             values["status"], batch.files_count, ok_pages, values["failed_files"], bill.charged, bill.overrun,
             bill.work_units)


def active_batch(db, user_id: int) -> Batch | None:
    return db.scalar(select(Batch).where(Batch.user_id == user_id, Batch.status.in_(ACTIVE)).order_by(Batch.id))
