"""Signed-in part of the site: upload, progress, results, downloads, history, plans and payment requests, account
and self-service deletion.

Every batch route looks the batch up by its public id inside what the signed-in user may see (web/company.py:
own personal batches; in a company, a member's own company batches or, for the owner, all company batches). A
foreign or unknown id is a 404.
"""

import io
import json
import logging
import math
import re
import secrets
import zipfile
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from docstruc.core.models import ExtractionResult
from docstruc.doctypes import DOCTYPES
from docstruc.exporters import csv_export, excel, i18n as xi18n, json_export, onec
from docstruc.exporters.tabular import document_rows, item_rows

from . import accounts
from . import balance as bal
from . import company
from . import crypto
from . import payments
from . import security as sec
from .deps import CSRF_COOKIE, SESSION_COOKIE, CsrfFailed, csrf_protect, get_db, origin_ok, require_user
from .i18n import translator
from .jobs import ACTIVE, BYOK_PROVIDERS, ByokKey, active_batch, run_batch
from .models import Batch, User
from .pdf_formats import KINDS, SCAN, TEXT, text_pdf
from .upload import FORM_OVERHEAD, LONG_RECEIPT_MAX_PARTS, UploadRejected, boundary_of, parse_upload, validate

log = logging.getLogger("skaner.web.cabinet")
router = APIRouter()

PUBLIC_ID = re.compile(r"[A-Za-z0-9_-]{20,40}")
DOCTYPE_LABELS = {"receipt": "d1", "invoice": "d2", "act": "d3", "waybill": "d4"}  # keys in locales
FORMATS = {
    "xlsx": ("fmt_xlsx", ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "1c": ("fmt_1c", "_1c.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "csv": ("fmt_csv", "_csv.zip", "application/zip"),
    "json": ("fmt_json", ".json", "application/json"),
}
# The two PDF formats (web/pdf_formats.py): label key, file name suffix. Only the chosen one can be downloaded.
PDF_FORMATS = {SCAN: ("fmt_pdf_scan", "_scan.pdf"), TEXT: ("fmt_pdf_text", "_text.pdf")}
KIND_LABELS = {"table": "kind_table", "scan": "kind_scan", "text": "kind_text"}
FORMAT_DESCS = {"table": "fd_table", "scan": "fd_scan", "text": "fd_text"}
STATUS_LABELS = {"queued": "b_queued", "running": "b_running", "done": "b_done", "failed": "b_failed"}
ERROR_TEXTS = {"service": "berr_service", "restart": "berr_restart", "file": "berr_file", "format": "berr_service",
               "key": "berr_key", "quota": "berr_quota"}
# A provider key as the providers issue them: a prefix and URL-safe characters (checked before the key is used).
BYOK_PREFIX = {"anthropic": "sk-ant-", "gemini": "AIza"}
BYOK_KEY_RE = re.compile(r"[A-Za-z0-9_\-]{20,256}")
# Rejected keys per user per day before the own-key path closes for them until tomorrow: the site must not become
# a free tester of stolen provider keys (each rejected key is a batch that failed with error_code "key").
BYOK_KEY_FAILS_PER_DAY = 5
HISTORY_LIMIT = 100

# Upload refusals: message key and HTTP status.
REFUSALS = {
    "no_pages": ("err_no_pages", 402), "active": ("err_active", 409), "hourly": ("err_hourly", 429),
    "daily": ("err_daily", 503), "trial_daily": ("err_trial_daily", 503), "busy": ("err_busy", 503), "batch_too_large": ("err_batch_too_large", 413),
    "too_many_files": ("err_too_many_files", 413), "no_files": ("err_no_files", 400), "bad_form": ("err_bad_form", 400),
    "long_parts": ("err_long_parts", 400), "trial_ip": ("err_trial_ip", 402), "byok_bad": ("err_byok_bad", 400), "key_required": ("err_key_required", 400), "byok_blocked": ("err_byok_blocked", 429),
    "scan_daily": ("err_scan_daily", 429),
    "no_pages_org": ("err_no_pages_org", 402), "org_blocked": ("err_org_blocked", 403),
}
FILE_REASONS = {
    "file_empty": "fr_empty", "file_too_large": "fr_too_large", "file_type": "fr_type", "file_heic": "fr_heic",
    "file_pixels": "fr_pixels", "file_unreadable": "fr_unreadable", "pdf_pages": "fr_pdf_pages",
    "pdf_unreadable": "fr_pdf_unreadable",
}


def kind_of(fmt: str) -> str | None:
    """table, scan or text; None for anything we do not offer."""
    if fmt in FORMATS:
        return "table"
    return KINDS.get(fmt)


def status_label(t, batch: Batch) -> str:
    if batch.status == "running" and batch.fmt == SCAN:
        return t("b_running_scan")  # nothing is recognised in a scan
    return t(STATUS_LABELS.get(batch.status, "b_failed"))


def fill(text: str, **values) -> str:
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def files_lang(lang: str) -> str:
    """Exported files and engine remarks: English for the English interface, otherwise Russian."""
    return "en" if lang == "en" else "ru"


# ---------- helpers bound to the app ----------

def _st(request: Request):
    return request.app.state


def _now(request: Request) -> datetime:
    return _st(request).clock()


def _ipf(request: Request) -> str:
    st = _st(request)
    return sec.ip_fp(st.settings, st.client_ip(request))


def _t(request: Request):
    return translator(_st(request).lang_of(request))


def purge_files(db: Session, now: datetime) -> int:
    """Scan PDFs past file_expires_at are removed; the history row (date, pages) stays."""
    n = db.execute(update(Batch).where(Batch.file_expires_at <= now, Batch.file_enc.is_not(None))
                   .values(file_enc=None)).rowcount
    db.commit()
    if n:
        log.info("expired scan files purged: %d", n)
    return n


def purge_results(db: Session, now: datetime) -> int:
    """Retention: finished batches past expires_at are deleted with their results, and scan files past their
    short life are removed (runs at start-up and on cabinet visits)."""
    n = db.execute(delete(Batch).where(Batch.expires_at < now, Batch.status.not_in(ACTIVE))).rowcount
    db.commit()
    if n:
        log.info("expired batches purged: %d", n)
    purge_files(db, now)
    payments.purge_requests(db, now)
    company.housekeeping(db, now)  # invitations: pending past their date become expired, old ones are deleted
    return n


def shell(request: Request, db: Session, user: User, nav: str, crumbs: list) -> dict:
    now = _now(request)
    ctx = company.context(db, user)
    b = bal.balance(ctx.wallet, now)  # in a company: the owner's pages, shared by everyone in it
    t = _t(request)
    plan_line = ""
    if b.plan_state in ("active", "used") and b.plan_expires_at is not None:
        plan_line = fill(t("bal_plan"), plan=t("plan_" + b.plan), date=f"{b.plan_expires_at:%d.%m.%Y}")
    return {
        "user": user, "bal": b, "days_left": b.days_left(now), "nav": nav, "crumbs": crumbs,
        "trial_blocked": False if ctx.is_member else bal.trial_blocked_by_ip(db, _st(request).settings, user,
                                                                               _ipf(request), now),
        "co_org": ctx.org, "co_member": ctx.is_member, "guest_days": _st(request).settings.guest_retention_days,
        "plan_line": plan_line,
        # The owner gets an "Админка" link in the menu; everyone else never sees that /admin exists.
        "is_admin": user.email_fp in getattr(_st(request), "admin_fps", ()),
    }


def own_batch(db: Session, user: User, public_id: str) -> Batch | None:
    """The batch if this user may see it (and so delete it), else None: see web/company.batch_scope."""
    if not PUBLIC_ID.fullmatch(public_id or ""):
        return None
    return company.visible_batch(db, company.context(db, user), public_id)


def not_found(request: Request, db: Session, user: User):
    t = _t(request)
    return _st(request).render(request, "app_message.html", 404, heading=t("nf_h"), text=t("nf_p"),
                               **shell(request, db, user, "docs", [("/app/history", t("nav_docs"))]))


def doctype_choices(t) -> list[tuple[str, str]]:
    return [(key, t(DOCTYPE_LABELS[key])) for key in DOCTYPES if key in DOCTYPE_LABELS]


def format_groups(t, scan_pages: int) -> list[tuple[str, list[dict]]]:
    """The format select: tables first, then the two PDFs. Each option carries its kind and a description."""
    def option(key: str, label_key: str, kind: str) -> dict:
        return {"key": key, "label": t(label_key), "kind": kind, "desc": fill(t(FORMAT_DESCS[kind]), n=scan_pages)}

    return [
        (t("up_fmt_tables"), [option(k, v[0], "table") for k, v in FORMATS.items()]),
        ("PDF", [option(k, v[0], KINDS[k]) for k, v in PDF_FORMATS.items() if k != SCAN]),
    ]


def upload_page(request: Request, db: Session, user: User, error: str = "", status: int = 200):
    t, s = _t(request), _st(request).settings
    mb = s.max_file_bytes // (1024 * 1024)
    limits = fill(t("up_limits"), files=s.max_files, mb=mb, pages=s.max_pdf_pages)
    groups = format_groups(t, s.free_scan_pages_day)
    invites = [fill(t("co_invite_from"), name=org.name) for _, org in
               company.invites_for_user(db, user.id, _now(request))]
    return _st(request).render(
        request, "app_upload.html", status, error=error, limits=limits, active=active_batch(db, user.id),
        co_invites=invites,
        doctypes=doctype_choices(t), format_groups=groups, first_desc=groups[0][1][0]["desc"],
        max_files=s.max_files, max_mb=mb, long_max=LONG_RECEIPT_MAX_PARTS,
        **shell(request, db, user, "up", [("", t("nav_up"))]),
    )


def refusal_text(request: Request, code: str, **values) -> str:
    t, s = _t(request), _st(request).settings
    key = REFUSALS[code][0]
    return fill(t(key), mb=s.max_batch_bytes // (1024 * 1024), files=s.max_files, parts=LONG_RECEIPT_MAX_PARTS,
                **values)


def file_text(request: Request, exc: UploadRejected) -> str:
    t, s = _t(request), _st(request).settings
    reason = fill(t(FILE_REASONS.get(exc.code, "fr_unreadable")), mb=s.max_file_bytes // (1024 * 1024),
                  pages=s.max_pdf_pages)
    return fill(t("err_file"), n=exc.index or "?", name=exc.name or "", reason=reason)


# ---------- pages ----------

def state_lines(t, b: bal.Balance, now: datetime) -> list[str]:
    """Plain sentences about the trial, the plan and top-up pages (plans and account pages)."""
    lines = []
    if b.trial_state == "active":
        lines.append(fill(t("pl_trial"), n=b.trial_left, d=b.days_left(now)))
    elif b.trial_state in ("expired", "used"):
        lines.append(t("pl_trial_over"))
    if b.plan_state in ("active", "used"):
        lines.append(fill(t("pl_plan"), plan=t("plan_" + b.plan), n=b.plan_left, q=b.plan_quota,
                          date=f"{b.plan_expires_at:%d.%m.%Y}"))
    elif b.plan_state == "expired":
        lines.append(fill(t("pl_plan_over"), plan=t("plan_" + b.plan), date=f"{b.plan_expires_at:%d.%m.%Y}"))
    else:
        lines.append(t("pl_none"))
    if b.paid_left > 0:
        lines.append(fill(t("pl_paid"), n=b.paid_left))
    return lines


def _touch(request: Request, db: Session, user: User) -> None:
    """Cabinet visit housekeeping: the trial grant and zeroing an expired plan's pages (with its ledger row)."""
    now, st = _now(request), _st(request)
    bal.ensure_trial(db, st.settings, user, _ipf(request), now)
    bal.expire_plan(db, user, now)
    ctx = company.context(db, user)
    if ctx.wallet is not None and ctx.wallet.id != user.id:
        bal.expire_plan(db, ctx.wallet, now)


def wallet_lines(request: Request, db: Session, user: User) -> list[str]:
    """Plans and account pages: inside a company, the company's (owner's) pages; outside, the user's own."""
    t, now = _t(request), _now(request)
    ctx = company.context(db, user)
    lines = state_lines(t, bal.balance(ctx.wallet, now), now)
    if ctx.in_org:
        lines.insert(0, fill(t("co_pages_of"), name=ctx.org.name))
        if ctx.is_member:
            lines.append(t("co_pay_owner"))
    return lines


DELETE_WORDS = ("удалить", "жою", "delete")  # any language: the page may have been switched in between


def account_page(request: Request, db: Session, user: User, error: str = "", status: int = 200):
    st, t, now = _st(request), _t(request), _now(request)
    lang_names = dict(st.lang_names)
    word = t("ac_del_word")
    return st.render(request, "account.html", status, user_lang=lang_names.get(user.lang or "", ""),
                     lines=wallet_lines(request, db, user), error=error, del_word=word,
                     del_label=fill(t("ac_del_label"), word=word),
                     stored=fill(t("ac_stored"), n=st.settings.retention_days),
                     **shell(request, db, user, "acc", [("", t("nav_settings"))]))


@router.get("/account")
@router.get("/app/account")
def account(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _touch(request, db, user)
    return account_page(request, db, user)


@router.post("/app/account/delete", dependencies=[Depends(csrf_protect)])
def delete_account(request: Request, confirm: str = Form(""), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    t = _t(request)
    if confirm.strip().lower() not in DELETE_WORDS:
        return account_page(request, db, user, fill(t("ac_del_err"), word=t("ac_del_word")), 400)
    if active_batch(db, user.id) is not None:
        return account_page(request, db, user, t("ac_del_active"), 409)
    refused = company.account_delete_refusal(db, user)
    if refused == "org_members":
        return account_page(request, db, user, t("ac_del_owner"), 409)
    if refused is not None:
        return account_page(request, db, user, t("ac_del_active"), 409)
    accounts.delete_user(db, user.id, _now(request))
    log.info("account deleted by its user")
    st = _st(request)
    response = RedirectResponse("/", status_code=303)
    st.clear_cookie(response, SESSION_COOKIE)
    st.rotate_csrf(request, response)
    request.state.session_changed = True
    return response


# ---------- plans and the payment request ----------

def plan_cards(t) -> list[dict]:
    return [{"key": k, "name": t("plan_" + k), "price": t("price_" + k), "pages": t("pages_" + k),
             "over": t("over_" + k)} for k in accounts.PLANS]


@router.get("/app/plans")
def plans(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not _st(request).settings.paid_plans:
        return not_found(request, db, user)
    _touch(request, db, user)
    t, now = _t(request), _now(request)
    return _st(request).render(request, "app_plans.html", cards=plan_cards(t),
                               lines=wallet_lines(request, db, user), pay_email=_st(request).settings.pay_email,
                               **shell(request, db, user, "plans", [("", t("nav_plans"))]))


REQUEST_ERRORS = {"plan": "rq_err_plan", "contact": "rq_err_contact", "text": "rq_err_text", "long": "rq_err_long",
                  "email": "rq_err_email", "many": "rq_err_many"}


def request_page(request: Request, db: Session, user: User, plan: str, values: dict | None = None,
                 error: str = "", status: int = 200):
    t = _t(request)
    return _st(request).render(
        request, "app_request.html", status, cards=plan_cards(t), chosen=plan if plan in accounts.PLANS else "biz",
        values=values or {}, error=error, company_max=payments.COMPANY_MAX, contact_max=payments.CONTACT_MAX,
        comment_max=payments.COMMENT_MAX,
        **shell(request, db, user, "plans", [("/app/plans", t("nav_plans")), ("", t("rq_h"))]))


@router.get("/app/plans/request")
def request_form(request: Request, plan: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not _st(request).settings.paid_plans:
        return not_found(request, db, user)
    return request_page(request, db, user, plan)


@router.post("/app/plans/request", dependencies=[Depends(csrf_protect)])
def request_submit(request: Request, background: BackgroundTasks, plan: str = Form(""), company: str = Form(""),
                   contact: str = Form(""), comment: str = Form(""), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    st, t, now = _st(request), _t(request), _now(request)
    if not st.settings.paid_plans:
        return not_found(request, db, user)
    # Shown back in the form on an error (escaped by the template); cut so a huge value is not echoed.
    values = {"company": company[:payments.COMPANY_MAX * 2], "contact": contact[:payments.CONTACT_MAX * 2],
              "comment": comment[:payments.COMMENT_MAX * 2]}
    try:
        form = payments.parse_form(plan, company, contact, comment)
    except payments.FormError as e:
        return request_page(request, db, user, plan, values, t(REQUEST_ERRORS[e.code]), 400)
    ipf = _ipf(request)
    if payments.over_limit(db, user.id, ipf, now):
        return request_page(request, db, user, plan, values, t(REQUEST_ERRORS["many"]), 429)
    provider = st.payments
    request_id = provider.submit(db, user, form, ipf, now)
    background.add_task(provider.notify, request_id, payments.notice_text(user.account_code or "-", form), now)
    return st.render(request, "app_request_done.html",
                     **shell(request, db, user, "plans", [("/app/plans", t("nav_plans")), ("", t("rq_done_h"))]))


@router.get("/app")
def app_home(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    now = _now(request)
    _touch(request, db, user)
    purge_results(db, now)
    return upload_page(request, db, user)


@router.get("/app/history")
def history(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    t = _t(request)
    purge_results(db, _now(request))
    ctx = company.context(db, user)
    rows = db.scalars(select(Batch).where(company.batch_scope(ctx))
                      .order_by(Batch.created_at.desc(), Batch.id.desc()).limit(HISTORY_LIMIT)).all()
    # The owner sees whose batch it is: "you" or the author's account code (never anything else about them).
    codes = company.author_codes(db, {b.user_id for b in rows if b.user_id != user.id}) if ctx.is_owner else {}
    items = []
    for b in rows:
        kind = kind_of(b.fmt) or "table"
        if kind == "scan":  # free: show the pages of the PDF, not the (always 0) pages charged
            pages = b.docs_found if b.status == "done" else b.pages_estimated
        else:
            pages = b.pages_charged if b.status in ("done", "failed") else b.pages_estimated
        items.append({
            "id": b.public_id, "date": b.created_at.strftime("%d.%m.%Y %H:%M"), "kind": t(KIND_LABELS[kind]),
            "doctype": t(DOCTYPE_LABELS.get(b.doctype, "d1")) if kind == "table" else "—", "files": b.files_count,
            "pages": pages, "status": status_label(t, b), "state": b.status, "review": b.needs_review,
            "who": t("co_you") if b.user_id == user.id else (codes.get(b.user_id) or "—"),
        })
    return _st(request).render(request, "app_history.html", items=items, show_who=ctx.is_owner,
                               **shell(request, db, user, "docs", [("", t("nav_docs"))]))


def _value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _label(col: str, flang: str) -> str:
    if flang == "ru":
        return xi18n.column(col, "ru")
    return col.replace("_", " ").replace(".", " · ")


def document_view(r: ExtractionResult, flang: str) -> dict:
    failed = not r.data and any(i.message.startswith("Extraction failed") for i in r.issues)
    flagged = r.review_fields
    cols, rows = document_rows([r])
    row = rows[0]
    fields = [
        {"label": _label(c, flang), "value": _value(row.get(c)), "flag": c in flagged}
        for c in cols if c not in ("file", "status") and (row.get(c) not in (None, "") or c in flagged)
    ]
    icols, irows = item_rows([r])
    icols = [c for c in icols if c != "file"]
    bad_lines = set()
    for i in r.issues:
        parts = i.field.split(".")
        if i.severity == "error" and len(parts) > 1 and parts[0] == "items" and parts[1].isdigit():
            bad_lines.add(int(parts[1]) + 1)
    items = [{"cells": [_value(ir.get(c)) for c in icols], "flag": ir["line"] in bad_lines} for ir in irows]
    issues = [
        {"field": xi18n.field(i.field, flang), "message": xi18n.message(i.message, flang), "check": i.severity == "error"}
        for i in r.issues if r.data  # failed / empty files get one plain line instead
    ]
    return {
        "name": r.source, "found": bool(r.data), "failed": failed, "review": r.needs_review,
        "fields": fields, "item_cols": ["№" if c == "line" else _label(c, flang) for c in icols], "lines": items,
        "issues": issues,
    }


def load_results(request: Request, batch: Batch) -> list[ExtractionResult]:
    payload = crypto.decrypt(_st(request).settings, batch.public_id, batch.results_enc)
    return json_export.from_dict(payload["results"])


def file_minutes_left(batch: Batch, now: datetime) -> int:
    """Whole minutes (rounded up) a scan PDF can still be downloaded; 0 when it is gone."""
    if batch.file_expires_at is None or batch.file_expires_at <= now:
        return 0
    return math.ceil((batch.file_expires_at - now).total_seconds() / 60)


@router.get("/app/batches/{public_id}")
def batch_page(public_id: str, request: Request, fb: str = "", user: User = Depends(require_user),
               db: Session = Depends(get_db)):
    now = _now(request)
    purge_files(db, now)
    batch = own_batch(db, user, public_id)
    if batch is None:
        return not_found(request, db, user)
    t, lang = _t(request), _st(request).lang_of(request)
    s = _st(request).settings
    kind = kind_of(batch.fmt) or "table"
    docs, unreadable = [], False
    if batch.status == "done" and kind == "table":
        try:
            docs = [document_view(r, files_lang(lang)) for r in load_results(request, batch)]
        except crypto.ResultsUnreadable:
            unreadable = True
            log.warning("stored results could not be decrypted")
    minutes = file_minutes_left(batch, now) if kind == "scan" else 0
    what = ""
    if kind == "scan":
        what = fill(t("b_scan_what"), n=batch.docs_found)
    elif kind == "text":
        what = fill(t("b_text_what"), n=batch.docs_found + batch.failed_files)
    title = t(DOCTYPE_LABELS.get(batch.doctype, "d1")) if kind == "table" else t(KIND_LABELS[kind])
    author = ""
    if batch.user_id != user.id:  # only the company owner can open a colleague's batch
        code = company.author_codes(db, {batch.user_id}).get(batch.user_id)
        author = fill(t("b_author"), who=code or "—")
    return _st(request).render(
        request, "app_batch.html", batch=batch, docs=docs, unreadable=unreadable, title=title, kind=kind,
        date=batch.created_at.strftime("%d.%m.%Y %H:%M"), status_label=status_label(t, batch),
        progress=fill(t("b_progress"), done=batch.files_done, total=batch.files_count),
        error_text=t(ERROR_TEXTS.get(batch.error_code or "", "berr_internal")),
        formats=[(k, t(v[0])) for k, v in FORMATS.items()], active=batch.status in ACTIVE,
        fb=fb if fb in ("sent", "bad", "many") else "", fb_kinds=[(k, t("fb_k_" + k)) for k in payments.FEEDBACK_KINDS],
        retention_days=s.retention_days, what=what, file_gone=kind == "scan" and minutes == 0, author=author,
        file_left=fill(t("b_file_left"), n=minutes), file_gone_text=fill(t("b_file_gone"), n=s.scan_file_minutes),
        **shell(request, db, user, "docs", [("/app/history", t("nav_docs")), ("", f"{title} · {batch.created_at:%d.%m.%Y}")]),
    )


@router.get("/app/batches/{public_id}/status")
def batch_status(public_id: str, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    batch = own_batch(db, user, public_id)
    if batch is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    t = _t(request)
    return JSONResponse({
        "status": batch.status, "done": batch.files_done, "total": batch.files_count,
        "label": status_label(t, batch),
    })


def _csv_zip(results: list[ExtractionResult], flang: str = "ru") -> bytes:
    """Two CSV tables; ";" and decimal commas for Russian-language files (what Excel in ru/kk Windows expects)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, (columns, rows) in (("documents.csv", document_rows(results)), ("items.csv", item_rows(results))):
            zf.writestr(name, "﻿" + csv_export.table_text(columns, rows, flang))  # BOM: Excel opens UTF-8 correctly
    return out.getvalue()


def export_bytes(results: list[ExtractionResult], fmt: str, lang: str) -> bytes:
    if fmt == "xlsx":
        return excel.to_bytes(results, lang=files_lang(lang))
    if fmt == "1c":
        return onec.to_bytes(results)
    if fmt == "csv":
        return _csv_zip(results, files_lang(lang))
    return json.dumps(json_export.to_dict(results), ensure_ascii=False, indent=2).encode("utf-8")


def file_gone(request: Request, db: Session, user: User):
    t, s = _t(request), _st(request).settings
    return _st(request).render(request, "app_message.html", 410, heading=t("gone_h"),
                               text=fill(t("b_file_gone"), n=s.scan_file_minutes),
                               **shell(request, db, user, "docs", [("/app/history", t("nav_docs"))]))


def pdf_download(request: Request, db: Session, user: User, batch: Batch):
    """The single file of a PDF batch: the stored scan (until it expires) or the text PDF laid out now."""
    settings = _st(request).settings
    try:
        if batch.fmt == SCAN:
            if file_minutes_left(batch, _now(request)) == 0 or batch.file_enc is None:
                return file_gone(request, db, user)
            content = crypto.decrypt_file(settings, batch.public_id, batch.file_enc)
        else:
            payload = crypto.decrypt(settings, batch.public_id, batch.results_enc)
            content = text_pdf(payload["pages"], files_lang(batch.lang))
    except crypto.ResultsUnreadable:
        log.warning("stored results could not be decrypted")
        return not_found(request, db, user)
    filename = f"skaner_{batch.created_at:%Y%m%d_%H%M}{PDF_FORMATS[batch.fmt][1]}"  # ours, never from user input
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@router.get("/app/batches/{public_id}/download")
def download(public_id: str, request: Request, fmt: str = "xlsx", user: User = Depends(require_user),
             db: Session = Depends(get_db)):
    batch = own_batch(db, user, public_id)
    if batch is None or batch.status != "done":
        return not_found(request, db, user)
    if batch.fmt in PDF_FORMATS:  # a PDF batch has exactly one file, in the format chosen at upload
        if fmt != batch.fmt:
            return not_found(request, db, user)
        return pdf_download(request, db, user, batch)
    if fmt not in FORMATS:
        return not_found(request, db, user)
    try:
        results = load_results(request, batch)
    except crypto.ResultsUnreadable:
        log.warning("stored results could not be decrypted")
        return not_found(request, db, user)
    content = export_bytes(results, fmt, batch.lang)
    _, suffix, media = FORMATS[fmt]
    filename = f"skaner_{batch.created_at:%Y%m%d_%H%M}_{batch.doctype}{suffix}"  # ours, never from user input
    return Response(content, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@router.post("/app/batches/{public_id}/feedback", dependencies=[Depends(csrf_protect)])
def batch_feedback(public_id: str, request: Request, background: BackgroundTasks, kind: str = Form(""),
                   comment: str = Form(""), user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Something is wrong with this result: a short note to the owner. Only users who may see the batch can send it."""
    batch = own_batch(db, user, public_id)
    if batch is None:
        return not_found(request, db, user)
    st, now = _st(request), _now(request)
    back = f"/app/batches/{batch.public_id}"
    try:
        kind, comment = payments.parse_feedback(kind, comment)
    except payments.FormError:
        return RedirectResponse(f"{back}?fb=bad", status_code=303)
    ipf = sec.ip_fp(st.settings, st.client_ip(request))
    if payments.feedback_over_limit(db, user.id, ipf, now):
        return RedirectResponse(f"{back}?fb=many", status_code=303)
    request_id = st.payments.submit_feedback(db, user, kind, comment, batch.public_id, batch.doctype, batch.fmt, ipf, now)
    background.add_task(st.payments.notify, request_id, payments.feedback_notice_text(
        user.account_code or "-", kind, batch.public_id, batch.doctype, batch.fmt, comment), now)
    return RedirectResponse(f"{back}?fb=sent", status_code=303)


@router.post("/app/batches/{public_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_batch(public_id: str, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    batch = own_batch(db, user, public_id)
    if batch is None:
        return not_found(request, db, user)
    t = _t(request)
    if batch.status in ACTIVE:
        return _st(request).render(request, "app_message.html", 409, heading=t("b_running"),
                                   text=t("err_delete_active"),
                                   **shell(request, db, user, "docs", [("/app/history", t("nav_docs"))]))
    db.delete(batch)  # hard delete: the encrypted results go with the row; the ledger keeps the charge
    db.commit()
    log.info("batch deleted by user")
    return RedirectResponse("/app/history", status_code=303)


# ---------- upload ----------

class Refusal(Exception):
    def __init__(self, code: str, **values):
        super().__init__(code)
        self.code, self.values = code, values


def _refuse_page(request: Request, user_id: int, text: str, status: int):
    with _st(request).sessionmaker() as db:
        user = db.get(User, user_id)
        return upload_page(request, db, user, error=text, status=status)


def _precheck(request: Request, user_id: int) -> None:
    """Cheap read-only checks before the body is read (and before the CSRF token in it is checked):
    nothing is uploaded for a request that will be refused anyway. The format is inside the body, so a user
    who cannot start a charged batch is still let through while a free scan is possible; _admit decides then."""
    st, now = _st(request), _now(request)
    s = st.settings
    with st.sessionmaker() as db:
        user = db.get(User, user_id)
        if active_batch(db, user_id) is not None:
            raise Refusal("active")
        if bal.batches_last_hour(db, user_id, now) >= s.batches_per_hour:
            raise Refusal("hourly")
        # No refusal for missing pages here: the body may carry the user's own key, which needs no pages.


def _validate_all(raw_files, settings, gate):
    """Checks every file; at most a few files are decoded at once on the whole site (web/jobs.UploadGate)."""
    checked = []
    for raw in raw_files:
        if not gate.acquire_decoder():
            raise Refusal("busy")
        try:
            checked.append(validate(raw, settings))
        finally:
            gate.release_decoder()
        raw.data = b""  # the Document holds the bytes now
    return checked


def _admit(request: Request, user_id: int, doctype: str, fmt: str, one_document: bool, files_count: int,
           pages: int, byok: bool = False) -> tuple[int, str]:
    """Final checks and the batch row. One active batch per user: of two parallel uploads the older row wins.
    A free scan takes its pages from the scan allowance; every other format from the page balance, except a batch
    run with the user's own key (`byok`): it takes nothing from the balance or the site's daily cap."""
    st, now = _st(request), _now(request)
    s = st.settings
    scan = fmt == SCAN
    paid = not scan and not byok
    if paid and not s.paid_plans:  # free mode: nothing runs on the site's own key
        raise Refusal("key_required")
    with st.sessionmaker() as db:
        purge_results(db, now)
        user = db.get(User, user_id)
        ctx = company.context(db, user)
        wallet = ctx.wallet  # the user, or the company owner: whose pages this batch takes
        trial = False  # trial-funded: only trial pages in the wallet (bal.trial_funded), fixed on the batch
        if byok:
            rejected = db.scalar(select(func.count()).select_from(Batch).where(
                Batch.user_id == user_id, Batch.byok.is_(True), Batch.error_code == "key",
                Batch.created_at > now - timedelta(days=1)))
            if rejected >= BYOK_KEY_FAILS_PER_DAY:
                raise Refusal("byok_blocked")
        if paid:
            if wallet.blocked_at is not None:
                raise Refusal("org_blocked")
            b = bal.balance(wallet, now)
            total = b.total - (company.wallet_reserved(db, wallet.id) if ctx.in_org else 0)
            if ctx.is_member and pages > total:
                raise Refusal("no_pages_org", need=pages, left=max(0, total))
            if total <= 0 and bal.trial_blocked_by_ip(db, s, wallet, _ipf(request), now):
                raise Refusal("trial_ip")
            if pages > total:
                raise Refusal("no_pages", need=pages, left=max(0, total))
            if bal.daily_used(db, now) >= s.daily_pages:
                raise Refusal("daily")
            trial = bal.trial_funded(b)
            if trial and bal.daily_trial_used(db, now) >= s.trial_daily_pages:
                raise Refusal("trial_daily")
        elif scan and pages > bal.scan_left(db, s, user_id, now):
            raise Refusal("scan_daily", limit=s.free_scan_pages_day, left=bal.scan_left(db, s, user_id, now))
        if active_batch(db, user_id) is not None:
            raise Refusal("active")
        if bal.batches_last_hour(db, user_id, now) >= s.batches_per_hour:
            raise Refusal("hourly")
        batch = Batch(
            public_id=secrets.token_urlsafe(18), user_id=user_id, org_id=ctx.org.id if ctx.in_org else None,
            created_by_user_id=user_id, wallet_user_id=wallet.id, doctype=doctype, fmt=fmt, one_document=one_document,
            trial_funded=trial, byok=byok, lang=st.lang_of(request), status="queued", files_count=files_count, files_done=0, pages_estimated=pages,
            pages_charged=0, work_units=0, docs_found=0, needs_review=0, failed_files=0, not_found=0,
            created_at=now, expires_at=now + timedelta(days=s.guest_retention_days if user.is_guest else s.retention_days),
        )
        db.add(batch)
        db.commit()
        if active_batch(db, user_id).id != batch.id:
            db.delete(batch)
            db.commit()
            raise Refusal("active")
        if paid and ctx.in_org:
            # Several people spend one wallet: counted again after our row is written, so of two parallel uploads
            # the one that checks last sees both and steps back (never more pages reserved than the wallet has).
            total = bal.balance(db.get(User, wallet.id, populate_existing=True), now).total
            if company.wallet_reserved(db, wallet.id) > total:
                db.delete(batch)
                db.commit()
                left = max(0, total - company.wallet_reserved(db, wallet.id))
                raise Refusal("no_pages_org" if ctx.is_member else "no_pages", need=pages, left=left)
        if scan:
            refused = bal.reserve_scan(db, s, user_id, pages, now)
            if refused is not None:
                db.delete(batch)
                db.commit()
                raise Refusal(refused, limit=s.free_scan_pages_day, left=bal.scan_left(db, s, user_id, now))
        elif paid:
            refused = bal.reserve_daily(db, s, pages, now, trial=trial)
            if refused is not None:
                db.delete(batch)
                db.commit()
                raise Refusal(refused)
        bal.record_batch(db, user_id, now)
        db.commit()
        return batch.id, batch.public_id


def _byok_from(fields: dict[str, str], kind: str) -> ByokKey | None:
    """The user's own key from the form, or None. Removed from `fields` at once so it lives in one place only.
    A local scan uses no model, so a key sent with it is ignored."""
    provider, key = fields.pop("byok_provider", ""), fields.pop("byok_key", "").strip()
    if not key or kind == "scan":
        return None
    if provider not in BYOK_PROVIDERS or not BYOK_KEY_RE.fullmatch(key) or not key.startswith(BYOK_PREFIX[provider]):
        raise Refusal("byok_bad")
    return ByokKey(provider, key)


def _unsubmitted(request: Request, batch_id: int, pages: int) -> None:
    with _st(request).sessionmaker() as db:
        batch = db.get(Batch, batch_id)
        if batch is not None:
            if batch.fmt == SCAN:
                bal.adjust_scan(db, batch.user_id, bal.day_key(batch.created_at), -pages)
            elif not batch.byok:
                bal.adjust_daily(db, bal.day_key(batch.created_at), -pages, trial=batch.trial_funded)
            batch.status, batch.error_code, batch.finished_at = "failed", "internal", _now(request)
            db.commit()


@router.post("/app/upload")
async def upload(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    st = _st(request)
    s = st.settings
    cookie = request.cookies.get(CSRF_COOKIE)
    if not origin_ok(request) or not sec.valid_token_shape(cookie):
        raise CsrfFailed()
    user_id = user.id
    # The sign-in check left a transaction (a pooled connection) open on the request's session; it would be held
    # for the whole upload. Everything below opens its own short sessions, so give the connection back now: many
    # slow uploads must not use up the pool and make every other page fail.
    db.close()
    boundary = boundary_of(request.headers.get("content-type", ""))
    length = request.headers.get("content-length")
    if length is None:
        return PlainTextResponse("Length Required", status_code=411)
    if boundary is None or not length.isdigit():
        return await run_in_threadpool(_refuse_page, request, user_id, refusal_text(request, "bad_form"), 400)
    size = int(length)
    if size > s.max_batch_bytes + FORM_OVERHEAD:
        return await run_in_threadpool(_refuse_page, request, user_id, refusal_text(request, "batch_too_large"), 413)
    try:
        await run_in_threadpool(_precheck, request, user_id)
    except Refusal as r:
        return await run_in_threadpool(_refuse_page, request, user_id, refusal_text(request, r.code, **r.values),
                                       REFUSALS[r.code][1])
    # One upload per user is received and checked at a time: parallel ones would all pass the checks above and be
    # refused as "active" only after their files were decoded (memory a 1 KB image can multiply, web/jobs.UploadGate).
    refused = st.uploads.enter(user_id)
    if refused is not None:
        return await run_in_threadpool(_refuse_page, request, user_id, refusal_text(request, refused),
                                       REFUSALS[refused][1])
    ticket = st.memory.reserve(user_id, size)
    if ticket is None:
        st.uploads.leave(user_id)
        return await run_in_threadpool(_refuse_page, request, user_id, refusal_text(request, "busy"), 503)

    documents: list = []
    handed_over = False
    byok = None
    try:
        try:
            parsed = await parse_upload(request.stream(), boundary, s, lambda v: sec.csrf_ok(s, cookie, v))
            doctype, fmt = parsed.fields.get("doctype", ""), parsed.fields.get("fmt", "")
            one_document = parsed.fields.get("one_document") == "1"
            kind = kind_of(fmt)
            if kind is None or kind == "scan":  # the free scan is not offered any more; old batches stay readable
                raise Refusal("bad_form")
            if kind != "table":  # the PDF formats have no document type and no "one document" mode
                doctype, one_document = "", False
            elif doctype not in DOCTYPES or doctype not in DOCTYPE_LABELS:
                raise Refusal("bad_form")
            byok = _byok_from(parsed.fields, kind)
            if not parsed.files:
                raise Refusal("no_files")
            checked = await run_in_threadpool(_validate_all, parsed.files, s, st.uploads)
            parsed.files.clear()
            documents = [doc for doc, _ in checked]
            pages = sum(p for _, p in checked)
            checked.clear()
            if one_document and pages > LONG_RECEIPT_MAX_PARTS:
                raise Refusal("long_parts")
            batch_id, public_id = await run_in_threadpool(
                _admit, request, user_id, doctype, fmt, one_document, len(documents), pages, byok is not None)
        except UploadRejected as exc:
            text = file_text(request, exc) if exc.index else refusal_text(request, exc.code)
            return await run_in_threadpool(_refuse_page, request, user_id, text, exc.status)
        except Refusal as r:
            return await run_in_threadpool(_refuse_page, request, user_id, refusal_text(request, r.code, **r.values),
                                           REFUSALS[r.code][1])
        log.info("batch accepted: kind=%s files=%d pages=%d", kind, len(documents), pages)
        try:
            await run_in_threadpool(st.executor.submit, run_batch, st.jobs, batch_id, documents, ticket, byok)
            handed_over = True  # run_batch now owns the files and the memory ticket
        except RuntimeError:  # executor shut down
            await run_in_threadpool(_unsubmitted, request, batch_id, pages)
            return await run_in_threadpool(_refuse_page, request, user_id, refusal_text(request, "busy"), 503)
        return RedirectResponse(f"/app/batches/{public_id}", status_code=303)
    finally:
        st.uploads.leave(user_id)
        if not handed_over:
            documents.clear()
            if byok is not None:
                byok.wipe()
            ticket.release()
