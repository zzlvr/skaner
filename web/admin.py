"""Owner admin (/admin): dashboard, users, payment requests, audit. Russian only (the owner reads Russian).

Who is admin: a signed-in user whose email fingerprint is one of the fingerprints of WEB_ADMIN_EMAILS (computed
at start-up in web/main.py; nothing in the database marks admins).
- Anyone else, signed in or not, gets the same 404 as an unknown address (also for POST and wrong methods).
- The sign-in must be fresh: the session was created in the last 12 hours, otherwise the owner signs in again.
- Every change is a POST with the CSRF token and Origin check, the admin check runs first (so a non-admin never
  learns more than 404), at most 60 admin POSTs per hour, and each one writes an audit row.
- An email typed into the search is only fingerprinted in memory: never stored, logged or shown back.
- Responses carry X-Robots-Tag: noindex and Cache-Control: no-store (web/main.py middleware).
"""

import logging
import os
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from . import accounts
from . import auth
from . import balance as bal
from . import company
from . import payments
from . import phone
from . import security as sec
from .accounts import PLAN_DAYS, PLAN_NAMES, PLANS
from .deps import CSRF_COOKIE, CsrfFailed, ReauthRequired, current_user, get_db, origin_ok
from .jobs import active_batch
from .models import AdminAudit, Batch, LedgerEntry, PaymentRequest, RateEvent, ScanUsage, User
from .pdf_formats import SCAN, TEXT

log = logging.getLogger("skaner.web.admin")
router = APIRouter(prefix="/admin")

FRESH = timedelta(hours=12)
POSTS_PER_HOUR = 60
BUCKET = "admin_post"
NOTE_MAX = 200
LIST_USERS, LIST_REQUESTS, LIST_AUDIT, LAST_BATCHES, LAST_LEDGER = 50, 200, 200, 20, 30

# ?done=... after an action: a fixed message, never text from the request.
DONE = {
    "plan": "Plan switched on.", "topup": "Pages added.", "extend": "Plan extended.",
    "end": "Plan ended.", "trial": "Trial changed.", "block": "User blocked.",
    "unblock": "User unblocked.", "request": "Request marked as handled.",
    "phone": "Phone binding removed: the user can confirm a number again.",
    "org": "User removed from the company.",
}
REASONS = {
    "trial_grant": "trial", "trial_bonus": "trial: phone bonus", "batch": "processing", "scan_free": "PDF scan (free)",
    "plan_grant": "plan switched on", "plan_expire": "plan expired", "topup": "top-up", "admin_adjust": "adjustment",
}
TRIAL_STATES = {"active": "running", "expired": "ended", "used": "pages used up", "none": "not given"}
PLAN_STATES = {"active": "active", "expired": "expired", "used": "pages used up", "none": "none"}
KINDS = {"pdf_scan": "PDF scan", "pdf_text": "PDF with text"}


DOCTYPE_NAMES = {"receipt": "Receipts", "invoice": "Invoices", "act": "Acts", "waybill": "Waybills"}


# ---------- access ----------

def _not_found() -> HTTPException:
    return HTTPException(status_code=404)


def require_admin(request: Request, user: User | None = Depends(current_user)) -> User:
    fps = request.app.state.admin_fps
    if user is None or not fps or user.email_fp not in fps:
        raise _not_found()
    created = getattr(request.state, "session_created_at", None)
    if created is None or request.app.state.clock() - created > FRESH:
        raise ReauthRequired()
    return user


def admin_post(request: Request, admin: User = Depends(require_admin), csrf_token: str = Form(""),
               db: Session = Depends(get_db)) -> User:
    """Admin first (404 for others), then CSRF and Origin, then the hourly limit."""
    settings = request.app.state.settings
    if not origin_ok(request) or not sec.csrf_ok(settings, request.cookies.get(CSRF_COOKIE), csrf_token):
        raise CsrfFailed()
    now = request.app.state.clock()
    key = admin.email_fp[:16]
    used = db.scalar(select(func.count()).select_from(RateEvent).where(
        RateEvent.bucket == BUCKET, RateEvent.key_fp == key, RateEvent.created_at > now - timedelta(hours=1))) or 0
    if used >= POSTS_PER_HOUR:
        raise HTTPException(status_code=429)
    db.add(RateEvent(bucket=BUCKET, key_fp=key, created_at=now))
    db.commit()
    return admin


def audit(db: Session, admin: User, action: str, target: User | None, detail: str, now: datetime) -> None:
    db.add(AdminAudit(admin_fp=admin.email_fp[:16], action=action[:24], target_user_id=target.id if target else None,
                      target_code=target.account_code if target else None, detail=detail[:200], created_at=now))
    db.commit()
    log.info("admin action: %s", action)


# ---------- helpers ----------

def _now(request: Request) -> datetime:
    return request.app.state.clock()


def _render(request: Request, name: str, admin: User, status: int = 200, **ctx):
    return request.app.state.render(request, name, status, user=admin, **ctx)


def _int(raw: str, lo: int, hi: int) -> int | None:
    s = (raw or "").strip()
    if not re.fullmatch(r"[0-9]{1,7}", s):
        return None
    n = int(s)
    return n if lo <= n <= hi else None


def _note(raw: str) -> str | None:
    """The owner's note: one line, up to 200 characters, no control characters, no email. None when refused."""
    try:
        return payments._clean(raw or "", NOTE_MAX)
    except payments.FormError:
        return None


def _dt(v: datetime | None) -> str:
    return f"{v:%d.%m.%Y %H:%M}" if v else "—"


def _target(db: Session, code: str) -> User:
    user = accounts.by_code(db, code)
    if user is None:
        raise _not_found()
    return user


def _day_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day)


# ---------- dashboard ----------

@router.get("")
def dashboard(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    st, now = request.app.state, _now(request)
    s = st.settings
    start = _day_start(now)
    count = lambda q: db.scalar(q) or 0  # noqa: E731
    # The owner's own accounts (WEB_ADMIN_EMAILS) are left out of the summary, so the owner's tests do not look like
    # customers. The daily caps themselves still count every page (they limit real spending), see the note on the page.
    mine = select(User.id).where(User.email_fp.in_(st.admin_fps)) if st.admin_fps else select(User.id).where(False)
    others = lambda col: or_(col.is_(None), col.not_in(mine))  # noqa: E731  (a deleted author has no id: still a customer)
    day = bal.day_key(now)
    own_work = count(select(func.coalesce(func.sum(
        case((Batch.status.in_(("queued", "running")), Batch.pages_estimated), else_=Batch.work_units)), 0))
        .where(Batch.created_at >= start, Batch.user_id.in_(mine), Batch.fmt != SCAN))
    own_trial = count(select(func.coalesce(func.sum(
        case((Batch.status.in_(("queued", "running")), Batch.pages_estimated), else_=Batch.work_units)), 0))
        .where(Batch.created_at >= start, Batch.user_id.in_(mine), Batch.fmt != SCAN, Batch.trial_funded.is_(True)))
    own_scan = count(select(func.coalesce(func.sum(ScanUsage.pages), 0))
                     .where(ScanUsage.day == day, ScanUsage.user_id.in_(mine)))
    stats = {
        "users": count(select(func.count()).select_from(User).where(User.id.not_in(mine))),
        "users_today": count(select(func.count()).select_from(User)
                             .where(User.created_at >= start, User.id.not_in(mine))),
        "batches_today": count(select(func.count()).select_from(Batch)
                               .where(Batch.created_at >= start, others(Batch.user_id))),
        "failed_today": count(select(func.count()).select_from(Batch).where(
            Batch.created_at >= start, Batch.status == "failed", others(Batch.user_id))),
        "pages_today": max(0, bal.daily_used(db, now) - own_work), "pages_cap": s.daily_pages,
        "trial_today": max(0, bal.daily_trial_used(db, now) - own_trial), "trial_cap": s.trial_daily_pages,
        "scan_today": max(0, bal.scan_site_used(db, now) - own_scan), "scan_cap": s.scan_pages_day,
        "charged_today": count(select(func.coalesce(func.sum(LedgerEntry.charged), 0))
                               .where(LedgerEntry.created_at >= start, others(LedgerEntry.user_id))),
        "overrun_today": count(select(func.coalesce(func.sum(LedgerEntry.overrun), 0))
                               .where(LedgerEntry.created_at >= start, others(LedgerEntry.user_id))),
        "pending": count(select(func.count()).select_from(PaymentRequest).where(PaymentRequest.status == "new")),
    }
    since = now - timedelta(days=30)
    q = db.execute(
        select(Batch.doctype, func.count(), func.coalesce(func.sum(Batch.docs_found), 0),
               func.coalesce(func.sum(Batch.needs_review), 0), func.coalesce(func.sum(Batch.failed_files), 0))
        .where(Batch.created_at >= since, Batch.fmt.not_in((SCAN, TEXT)), Batch.status.in_(("done", "failed")),
               others(Batch.user_id))
        .group_by(Batch.doctype)).all()
    quality = [{"name": DOCTYPE_NAMES.get(d, d or "—"), "batches": n, "docs": docs, "review": rev, "failed": failed,
                "share": f"{100 * rev / docs:.0f}%" if docs else "—"} for d, n, docs, rev, failed in q]
    stats["feedback_30d"] = count(select(func.count()).select_from(PaymentRequest).where(
        PaymentRequest.plan == "feedback", PaymentRequest.created_at >= since, others(PaymentRequest.user_id)))
    config = [
        ("Telegram (request notices)", bool(getattr(st.notifier, "configured", False))),
        ("Mail (sign-in codes, Resend)", bool(s.resend_api_key)),
        ("Recognition API key (ANTHROPIC_API_KEY)", bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())),
    ]
    return _render(request, "admin_home.html", admin, stats=stats, config=config, quality=quality, today=f"{now:%d.%m.%Y}",
                   nav="home")


# ---------- users ----------

@router.get("/users")
def users(request: Request, code: str = "", admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    error = ""
    if code:
        found = accounts.by_code(db, code)
        if found is not None:
            return RedirectResponse(f"/admin/users/{found.account_code}", status_code=303)
        error = "No user with this code."
    return users_page(request, db, admin, error, 404 if error else 200)


def users_page(request: Request, db: Session, admin: User, error: str = "", status: int = 200):
    now = _now(request)
    rows = db.scalars(select(User).order_by(User.created_at.desc(), User.id.desc()).limit(LIST_USERS)).all()
    items = []
    for u in rows:
        b = bal.balance(u, now)
        items.append({"code": u.account_code or "—", "created": _dt(u.created_at), "login": _dt(u.last_login_at),
                      "plan": PLAN_NAMES.get(u.plan or "", "—") + ("" if b.plan_state in ("active", "used")
                                                                      or not u.plan else " (expired)"),
                      "pages": b.total, "blocked": u.blocked_at is not None,
                      "me": u.email_fp in request.app.state.admin_fps})
    return _render(request, "admin_users.html", admin, status, items=items, error=error, nav="users")


@router.post("/users/find")
def find_by_email(request: Request, email: str = Form(""), admin: User = Depends(admin_post),
                  db: Session = Depends(get_db)):
    """The typed address lives only in this function's memory: normalised, fingerprinted, dropped."""
    settings = request.app.state.settings
    normalized = sec.normalize_email(email)
    del email
    found = None
    if normalized is not None:
        found = db.scalar(select(User).where(User.email_fp == sec.email_fp(settings, normalized)))
    del normalized
    audit(db, admin, "find_email", found, "found" if found else "not found", _now(request))
    if found is None:
        return users_page(request, db, admin, "No account for this email.", 404)
    return RedirectResponse(f"/admin/users/{found.account_code}", status_code=303)


def user_page(request: Request, db: Session, admin: User, target: User, error: str = "", status: int = 200,
              done: str = ""):
    now = _now(request)
    b = bal.balance(target, now)
    batches = db.scalars(select(Batch).where(Batch.user_id == target.id)
                         .order_by(Batch.created_at.desc(), Batch.id.desc()).limit(LAST_BATCHES)).all()
    ledger = db.scalars(select(LedgerEntry).where(LedgerEntry.user_id == target.id)
                        .order_by(LedgerEntry.id.desc()).limit(LAST_LEDGER)).all()
    info = [
        ("Account code", target.account_code or "—"),
        ("Created", _dt(target.created_at)), ("Last sign-in", _dt(target.last_login_at)),
        ("Trial", f"{TRIAL_STATES[b.trial_state]}, {b.trial_left} pages left, until {_dt(b.trial_expires_at)}"),
        ("Plan", "none" if not target.plan else
         f"{PLAN_NAMES.get(target.plan, target.plan)}: {PLAN_STATES[b.plan_state]}, {b.plan_left} left "
         f"of {target.plan_quota} pages, until {_dt(target.plan_expires_at)}"),
        ("Extra pages (no expiry)", str(b.paid_left)), ("Available in total", str(b.total)),
        ("Blocked", _dt(target.blocked_at) if target.blocked_at else "no"),
    ]
    # Company (web/company.py): name, role, size; for an owner the account codes of the people in it.
    membership = company.membership_of(db, target.id)
    org_member = False
    if membership is not None:
        org = db.get(company.Organization, membership.org_id)
        people = company.members(db, membership.org_id)
        info.append(("Company", org.name if org else "—"))
        info.append(("Role in the company", "owner" if membership.role == company.OWNER else "member"))
        info.append(("People in the company", str(len(people))))
        if membership.role == company.OWNER:
            info.append(("Member codes", ", ".join(u.account_code or "—" for m, u in people
                                                       if m.role == company.MEMBER) or "—"))
        org_member = membership.role == company.MEMBER
    else:
        info.append(("Company", "none"))
    # Phone confirmation: only yes/no is shown, never the fingerprint.
    show_phone = request.app.state.settings.phone_verify or phone.has_binding(db, target)
    if show_phone:
        info.append(("Phone confirmed", "yes" if target.phone_fp else "no"))
    return _render(
        request, "admin_user.html", admin, status, target=target, is_me=target.email_fp in request.app.state.admin_fps, info=info, error=error, done=DONE.get(done, ""),
        plans=[(k, f"{PLAN_NAMES[k]} — {p.quota} pages, {p.price} ₸") for k, p in PLANS.items()],
        plan_days=PLAN_DAYS, max_topup=bal.MAX_TOPUP, max_trial_pages=bal.MAX_TRIAL_PAGES,
        max_trial_days=bal.MAX_TRIAL_DAYS, phone_reset=show_phone and phone.has_binding(db, target), max_extend=bal.MAX_EXTEND_DAYS, plan_active=b.plan_state != "none"
        and bal.plan_active(target, now), is_self=target.id == admin.id, nav="users", org_member=org_member,
        batches=[{"date": _dt(x.created_at), "kind": KINDS.get(x.fmt, "Table " + x.fmt), "doctype": x.doctype or "—",
                  "pages": x.pages_charged if x.status in ("done", "failed") else x.pages_estimated,
                  "status": x.status, "error": x.error_code or ""} for x in batches],
        ledger=[{"date": _dt(r.created_at), "reason": REASONS.get(r.reason, r.reason), "trial": r.trial_delta,
                 "plan": r.plan_delta, "paid": r.paid_delta, "charged": r.charged, "overrun": r.overrun,
                 "note": r.note or ""} for r in ledger],
    )


@router.get("/users/{code}")
def user_detail(code: str, request: Request, done: str = "", admin: User = Depends(require_admin),
                db: Session = Depends(get_db)):
    return user_page(request, db, admin, _target(db, code), done=done)


def _back(target: User, done: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/users/{target.account_code}?done={done}", status_code=303)


@router.post("/users/{code}/plan")
def act_plan(code: str, request: Request, plan: str = Form(""), note: str = Form(""),
             admin: User = Depends(admin_post), db: Session = Depends(get_db)):
    target, now = _target(db, code), _now(request)
    clean = _note(note)
    if plan not in PLANS or clean is None:
        return user_page(request, db, admin, target, "Choose a plan; the note is up to 200 characters, no email.", 400)
    bal.grant_plan(db, target, plan, clean or f"plan {PLAN_NAMES[plan]}", now)
    audit(db, admin, "plan_grant", target, f"plan={plan} quota={PLANS[plan].quota} until={target.plan_expires_at:%Y-%m-%d}",
          now)
    return _back(target, "plan")


@router.post("/users/{code}/topup")
def act_topup(code: str, request: Request, pages: str = Form(""), note: str = Form(""),
              admin: User = Depends(admin_post), db: Session = Depends(get_db)):
    target, now = _target(db, code), _now(request)
    n, clean = _int(pages, 1, bal.MAX_TOPUP), _note(note)
    if n is None or clean is None:
        return user_page(request, db, admin, target,
                         f"Pages: a whole number from 1 to {bal.MAX_TOPUP}; the note is up to 200 characters, no email.", 400)
    bal.topup(db, target, n, clean or "top-up", now)
    audit(db, admin, "topup", target, f"pages=+{n}", now)
    return _back(target, "topup")


@router.post("/users/{code}/extend")
def act_extend(code: str, request: Request, days: str = Form(""), note: str = Form(""),
               admin: User = Depends(admin_post), db: Session = Depends(get_db)):
    target, now = _target(db, code), _now(request)
    n, clean = _int(days, 1, bal.MAX_EXTEND_DAYS), _note(note)
    if n is None or clean is None:
        return user_page(request, db, admin, target, f"Days: 1 to {bal.MAX_EXTEND_DAYS}; the note is up to 200 characters.",
                         400)
    if not bal.extend_plan(db, target, n, clean or f"extended by {n} days", now):
        return user_page(request, db, admin, target, "No active plan: switch a plan on again.", 409)
    audit(db, admin, "plan_extend", target, f"days=+{n} until={target.plan_expires_at:%Y-%m-%d}", now)
    return _back(target, "extend")


@router.post("/users/{code}/end-plan")
def act_end_plan(code: str, request: Request, note: str = Form(""), admin: User = Depends(admin_post),
                 db: Session = Depends(get_db)):
    target, now = _target(db, code), _now(request)
    clean = _note(note)
    if clean is None:
        return user_page(request, db, admin, target, "The note is up to 200 characters, no email.", 400)
    if not bal.end_plan(db, target, clean or "plan ended", now):
        return user_page(request, db, admin, target, "The user has no plan.", 409)
    audit(db, admin, "plan_end", target, "plan ended", now)
    return _back(target, "end")


@router.post("/users/{code}/trial")
def act_trial(code: str, request: Request, pages: str = Form(""), days: str = Form(""), note: str = Form(""),
              admin: User = Depends(admin_post), db: Session = Depends(get_db)):
    target, now = _target(db, code), _now(request)
    p, d, clean = _int(pages, 0, bal.MAX_TRIAL_PAGES), _int(days, 0, bal.MAX_TRIAL_DAYS), _note(note)
    if p is None or d is None or clean is None:
        return user_page(request, db, admin, target, f"Pages: 0 to {bal.MAX_TRIAL_PAGES}, days: 0 to "
                         f"{bal.MAX_TRIAL_DAYS}; the note is up to 200 characters.", 400)
    bal.set_trial(db, target, p, d, clean or f"trial {p} pages / {d} days", now)
    audit(db, admin, "trial_set", target, f"pages={p} days={d}", now)
    return _back(target, "trial")


@router.post("/users/{code}/phone-reset")
def act_phone_reset(code: str, request: Request, admin: User = Depends(admin_post), db: Session = Depends(get_db)):
    """For people who changed their number: removes users.phone_fp and the number's "already had a trial" record,
    so the number (and this account) can be confirmed again. Pages already given are not touched."""
    target, now = _target(db, code), _now(request)
    if not phone.reset(db, target):
        return user_page(request, db, admin, target, "No phone is bound.", 409)
    audit(db, admin, "phone_reset", target, "phone binding removed", now)
    return _back(target, "phone")


@router.post("/users/{code}/leave-org")
def act_leave_org(code: str, request: Request, admin: User = Depends(admin_post), db: Session = Depends(get_db)):
    """Take a member (never an owner) out of their company: access to the company stops at once."""
    target, now = _target(db, code), _now(request)
    if not company.remove_user(db, target.id):
        return user_page(request, db, admin, target, "The user is not a company member.", 409)
    audit(db, admin, "org_remove", target, "removed from the company", now)
    return _back(target, "org")


@router.post("/users/{code}/block")
def act_block(code: str, request: Request, admin: User = Depends(admin_post), db: Session = Depends(get_db)):
    target, now = _target(db, code), _now(request)
    if target.id == admin.id:
        return user_page(request, db, admin, target, "You cannot block your own account.", 409)
    target.blocked_at = now
    db.commit()
    auth.revoke_all_sessions(db, target.id)
    audit(db, admin, "block", target, "blocked", now)
    return _back(target, "block")


@router.post("/users/{code}/unblock")
def act_unblock(code: str, request: Request, admin: User = Depends(admin_post), db: Session = Depends(get_db)):
    target, now = _target(db, code), _now(request)
    target.blocked_at = None
    db.commit()
    audit(db, admin, "unblock", target, "unblocked", now)
    return _back(target, "unblock")


@router.post("/users/{code}/delete")
def act_delete(code: str, request: Request, confirm: str = Form(""), admin: User = Depends(admin_post),
               db: Session = Depends(get_db)):
    target, now = _target(db, code), _now(request)
    if target.id == admin.id:
        return user_page(request, db, admin, target, "Delete your own account on the Account page.", 409)
    if accounts.clean_account_code(confirm) != target.account_code:
        return user_page(request, db, admin, target, "Enter the account code to delete.", 400)
    if active_batch(db, target.id) is not None:
        return user_page(request, db, admin, target, "A batch is running: delete after it ends.", 409)
    refused = company.account_delete_refusal(db, target)
    if refused == "org_members":
        return user_page(request, db, admin, target, "This is a company owner with members: remove them from the "
                         "company first.", 409)
    if refused is not None:
        return user_page(request, db, admin, target, "A batch is running on this account: delete after it "
                         "ends.", 409)
    code_text = target.account_code
    audit(db, admin, "delete_user", target, f"deleted {code_text}", now)
    accounts.delete_user(db, target.id, now)
    return RedirectResponse("/admin/users", status_code=303)


# ---------- payment requests ----------

@router.get("/requests")
def requests_list(request: Request, done: str = "", admin: User = Depends(require_admin),
                  db: Session = Depends(get_db)):
    settings, now = request.app.state.settings, _now(request)
    payments.purge_requests(db, now)
    rows = db.execute(select(PaymentRequest, User.account_code).join(User, User.id == PaymentRequest.user_id)
                      .order_by(PaymentRequest.created_at.desc(), PaymentRequest.id.desc()).limit(LIST_REQUESTS)).all()
    items = []
    for row, code in rows:
        data = payments.read(settings, row) or {}
        company_col, contact_col = data.get("company", ""), data.get("contact", "")
        if row.plan == "feedback":  # a note about a result: what is wrong / which upload
            company_col = payments.FEEDBACK_KINDS.get(data.get("kind", ""), "")
            contact_col = f"upload {data.get('batch', '')} ({data.get('doctype') or '-'}, {data.get('fmt', '')})"
        label = {"question": "Question", "feedback": "Problem with a result"}.get(row.plan, row.plan)
        items.append({
            "id": row.id, "code": code or "—", "date": _dt(row.created_at), "plan": PLAN_NAMES.get(row.plan, label),
            "company": company_col, "contact": contact_col, "comment": data.get("comment", ""),
            "unreadable": not data, "status": row.status, "done_at": _dt(row.done_at),
            "notified": row.notified_at is not None,
        })
    return _render(request, "admin_requests.html", admin, items=items, done=DONE.get(done, ""), nav="requests")


@router.post("/requests/{request_id}/done")
def request_done(request_id: str, request: Request, admin: User = Depends(admin_post), db: Session = Depends(get_db)):
    rid = _int(request_id, 1, 10**7 - 1)
    row = db.get(PaymentRequest, rid) if rid else None
    if row is None:
        raise _not_found()
    now = _now(request)
    if row.status != "done":
        row.status, row.done_at = "done", now
        db.commit()
    audit(db, admin, "request_done", db.get(User, row.user_id), f"request #{row.id}", now)
    return RedirectResponse("/admin/requests?done=request", status_code=303)


# ---------- audit ----------

@router.get("/audit")
def audit_list(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).limit(LIST_AUDIT)).all()
    items = [{"date": _dt(r.created_at), "action": r.action, "code": r.target_code or "—", "detail": r.detail,
              "exists": r.target_user_id is not None} for r in rows]
    return _render(request, "admin_audit.html", admin, items=items, nav="audit")
