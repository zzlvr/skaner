"""Company cabinet pages (logic and rules in web/company.py).

- GET /app/company: create a company, invitations addressed to me; in a company: shared pages, people, leave; the
  owner also invites (link or account code), cancels invitations, removes members, renames and deletes.
- Every change is a POST with the CSRF token. Owner-only actions answer 404 to everyone else (like an unknown
  address), and ids in forms are looked up inside the user's own company only.
- GET/POST /join/<token>: the one-time invitation link. Signed-out visitors go through the sign-in and come back
  to the same address (the usual `next`). An unknown, used, cancelled or expired link shows one neutral page.
"""

import logging
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from . import company
from .cabinet import DELETE_WORDS, _now, _st, _t, _touch, fill, shell, wallet_lines
from .deps import csrf_protect, get_db, require_user
from .models import User

log = logging.getLogger("skaner.web.company")
router = APIRouter()

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32}")
ID_RE = re.compile(r"[0-9]{1,9}")
# ?done=... after an action: a fixed message key, never text from the request.
DONE = {"created": "co_done_created", "renamed": "co_done_renamed", "removed": "co_done_removed",
        "left": "co_done_left", "deleted": "co_done_deleted", "cancelled": "co_done_cancelled",
        "joined": "co_done_joined", "declined": "co_done_declined", "sent": "co_code_sent"}


def _404() -> HTTPException:
    return HTTPException(status_code=404)


def _id(raw: str) -> int | None:
    return int(raw) if ID_RE.fullmatch((raw or "").strip()) else None


def _back(done: str) -> RedirectResponse:
    return RedirectResponse(f"/app/company?done={done}", status_code=303)


def company_page(request: Request, db: Session, user: User, error: str = "", status: int = 200, link: str = "",
                 done: str = "", name_value: str = ""):
    st, t, now = _st(request), _t(request), _now(request)
    s = st.settings
    ctx = company.context(db, user)
    view = {"in_org": ctx.in_org, "is_owner": ctx.is_owner, "link": link, "error": error,
            "done": t(DONE[done]) if done in DONE else "", "name_value": name_value[:company.NAME_MAX * 2],
            "name_max": company.NAME_MAX, "limits_note": fill(t("co_limits"), trial=s.org_max_trial,
                                                              paid=s.org_max_paid)}
    if not ctx.in_org:
        view["invites"] = [{"id": inv.id, "text": fill(t("co_invite_from"), name=org.name),
                            "until": fill(t("co_until"), date=f"{inv.expires_at:%d.%m.%Y}")}
                           for inv, org in company.invites_for_user(db, user.id, now)]
    else:
        org = ctx.org
        people = company.members(db, org.id)
        view.update(
            org=org, lines=wallet_lines(request, db, user),
            seats=fill(t("co_seats"), n=len(people), max=company.max_members(s, ctx.wallet, now)),
            members=[{"id": m.id, "label": t("co_you") if u.id == user.id else (u.account_code or "—"),
                      "role": t("co_role_" + m.role), "since": f"{m.created_at:%d.%m.%Y}",
                      "removable": ctx.is_owner and m.role == company.MEMBER} for m, u in people],
            del_word=t("ac_del_word"), del_label=fill(t("co_del_label"), word=t("ac_del_word")),
        )
        if ctx.is_owner:
            view["pending"] = [{"id": inv.id,
                                "what": t("co_by_link") if inv.kind == "link" else fill(t("co_by_code"),
                                                                                        code=inv.target_code or ""),
                                "until": fill(t("co_until"), date=f"{inv.expires_at:%d.%m.%Y}")}
                               for inv in company.live_for_org(db, org.id, now)]
    return st.render(request, "app_company.html", status, **view,
                     **shell(request, db, user, "co", [("", t("nav_company"))]))


def _owner_ctx(db: Session, user: User) -> company.Ctx:
    ctx = company.context(db, user)
    if not ctx.is_owner:
        raise _404()
    return ctx


@router.get("/app/company")
def company_get(request: Request, done: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    _touch(request, db, user)
    company.housekeeping(db, _now(request))
    return company_page(request, db, user, done=done)


@router.post("/app/company/create", dependencies=[Depends(csrf_protect)])
def company_create(request: Request, name: str = Form(""), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    t = _t(request)
    try:
        clean = company.clean_name(name)
    except company.NameRefused:
        return company_page(request, db, user, t("co_err_name"), 400, name_value=name)
    if company.create_org(db, user, clean, _now(request)) is None:
        return company_page(request, db, user, t("co_err_already"), 409)
    return _back("created")


@router.post("/app/company/rename", dependencies=[Depends(csrf_protect)])
def company_rename(request: Request, name: str = Form(""), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    ctx = _owner_ctx(db, user)
    try:
        clean = company.clean_name(name)
    except company.NameRefused:
        return company_page(request, db, user, _t(request)("co_err_name"), 400)
    company.rename(db, ctx.org, clean)
    return _back("renamed")


@router.post("/app/company/delete", dependencies=[Depends(csrf_protect)])
def company_delete(request: Request, confirm: str = Form(""), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    ctx, t = _owner_ctx(db, user), _t(request)
    if confirm.strip().lower() not in DELETE_WORDS:
        return company_page(request, db, user, fill(t("ac_del_err"), word=t("ac_del_word")), 400)
    if company.delete_org(db, ctx.org) != "ok":
        return company_page(request, db, user, t("co_del_active"), 409)
    return _back("deleted")


@router.post("/app/company/leave", dependencies=[Depends(csrf_protect)])
def company_leave(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    outcome = company.leave(db, user)
    if outcome == "owner":
        return company_page(request, db, user, _t(request)("co_owner_leave"), 409)
    return _back("left") if outcome == "ok" else RedirectResponse("/app/company", status_code=303)


@router.post("/app/company/remove", dependencies=[Depends(csrf_protect)])
def company_remove(request: Request, member: str = Form(""), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    ctx = _owner_ctx(db, user)
    mid = _id(member)
    if mid is None or not company.remove_member(db, ctx.org, mid):
        raise _404()
    return _back("removed")


def _refusal(t, s, code: str) -> str:
    if code == "full":
        return fill(t("co_full"), trial=s.org_max_trial, paid=s.org_max_paid)
    return t({"many": "co_many", "bad_code": "co_code_bad"}[code])


@router.post("/app/company/invite-link", dependencies=[Depends(csrf_protect)])
def company_invite_link(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    ctx, st = _owner_ctx(db, user), _st(request)
    token, refused = company.create_link(db, st.settings, ctx.org, user, _now(request))
    if refused:
        return company_page(request, db, user, _refusal(_t(request), st.settings, refused), 429 if refused == "many"
                            else 409)
    return company_page(request, db, user, link=st.settings.base_url.rstrip("/") + "/join/" + token)


@router.post("/app/company/invite-code", dependencies=[Depends(csrf_protect)])
def company_invite_code(request: Request, code: str = Form(""), user: User = Depends(require_user),
                        db: Session = Depends(get_db)):
    ctx, st = _owner_ctx(db, user), _st(request)
    refused = company.create_by_code(db, st.settings, ctx.org, user, code, _now(request))
    if refused:
        status = {"many": 429, "full": 409}.get(refused, 400)
        return company_page(request, db, user, _refusal(_t(request), st.settings, refused), status)
    return _back("sent")


@router.post("/app/company/cancel", dependencies=[Depends(csrf_protect)])
def company_cancel(request: Request, invite: str = Form(""), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    ctx = _owner_ctx(db, user)
    iid = _id(invite)
    if iid is None or not company.cancel(db, ctx.org, iid, _now(request)):
        raise _404()
    return _back("cancelled")


ACCEPT_ERRORS = {"already": "co_already", "full": "co_full_join", "gone": "join_bad"}


@router.post("/app/company/answer", dependencies=[Depends(csrf_protect)])
def company_answer(request: Request, invite: str = Form(""), answer: str = Form(""),
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Accept or decline an invitation addressed to my account code."""
    now, t = _now(request), _t(request)
    iid = _id(invite)
    inv = company.code_invite(db, user, iid, now) if iid is not None else None
    if inv is None or answer not in ("accept", "decline"):
        raise _404()
    if answer == "decline":
        company.decline(db, inv)
        return _back("declined")
    outcome = company.accept(db, _st(request).settings, user, inv, now)
    if outcome != "ok":
        return company_page(request, db, user, t(ACCEPT_ERRORS[outcome]), 409)
    return _back("joined")


# ---------- the invitation link ----------

def join_page(request: Request, db: Session, user: User, token: str, error: str = "", status: int = 200):
    st, t = _st(request), _t(request)
    inv = company.link_invite(db, token, _now(request)) if TOKEN_RE.fullmatch(token or "") else None
    org = db.get(company.Organization, inv.org_id) if inv is not None else None
    ctx = shell(request, db, user, "co", [("/app/company", t("nav_company")), ("", t("join_h"))])
    if org is None:
        return st.render(request, "app_join.html", 404, valid=False, **ctx)
    return st.render(request, "app_join.html", status, valid=True, token=token, error=error,
                     invite_text=fill(t("co_invite_from"), name=org.name),
                     in_org=company.membership_of(db, user.id) is not None, **ctx)


@router.get("/join/{token}")
def join_get(token: str, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _touch(request, db, user)
    return join_page(request, db, user, token)


@router.post("/join/{token}", dependencies=[Depends(csrf_protect)])
def join_post(token: str, request: Request, answer: str = Form(""), user: User = Depends(require_user),
              db: Session = Depends(get_db)):
    now, t = _now(request), _t(request)
    inv = company.link_invite(db, token, now) if TOKEN_RE.fullmatch(token or "") else None
    if inv is None or answer not in ("accept", "decline"):
        return join_page(request, db, user, token)
    if company.membership_of(db, user.id) is not None:  # nothing to decide, and the link stays for its addressee
        return join_page(request, db, user, token, t("co_already"), 409)
    if answer == "decline":
        company.decline(db, inv)
        return RedirectResponse("/app/company?done=declined", status_code=303)
    outcome = company.accept(db, _st(request).settings, user, inv, now)
    if outcome != "ok":
        return join_page(request, db, user, token, t(ACCEPT_ERRORS[outcome]), 409)
    return _back("joined")
