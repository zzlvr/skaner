"""Routes of the phone confirmation (logic in web/phone.py). All of them are 404 unless WEB_PHONE_VERIFY=1.

- GET /app/phone: the state, works by plain reload (no JavaScript needed).
- POST /app/phone (CSRF): a new one-time Telegram link; the page shows it once (only its hash is stored).
- POST /internal/phone-verified: called by our bot only. Not part of the public site: no session or CSRF, but a
  valid HMAC signature (PHONE_VERIFY_SECRET), a fresh timestamp (±5 min) and a live one-time token are required.
  SECURITY: the reverse proxy must not expose /internal/ to the internet (nginx: `location /internal/ { deny all; }`
  before the proxy_pass location); the bot reaches the app over localhost (WEB_INTERNAL_URL). Nothing about the
  request (token, fingerprint, signature) is logged.
"""

import json
import logging
from datetime import timezone

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from bot import phone_verify as proto

from . import phone
from .cabinet import _ipf, _now, _st, _t, _touch, fill, shell
from .deps import csrf_protect, get_db, require_user
from .models import User

log = logging.getLogger("skaner.web.phone")
router = APIRouter()

# HTTP status of each answer of the internal endpoint (the body is always {"status": <one word>}).
HTTP = {proto.OK: 200, proto.PHONE_USED: 200, proto.OTHER_PHONE: 200, proto.INVALID: 410, proto.DENIED: 403,
        proto.LIMITED: 429}
OUTCOME_TEXT = {proto.PHONE_USED: "ph_used", proto.OTHER_PHONE: "ph_other", proto.INVALID: "ph_expired",
                "expired": "ph_expired"}


def _enabled(request: Request) -> None:
    if not _st(request).settings.phone_verify:
        raise HTTPException(status_code=404)


def phone_page(request: Request, db: Session, user: User, link: str = "", error: str = "", status: int = 200):
    st, t, now = _st(request), _t(request), _now(request)
    s = st.settings
    ch = phone.current_challenge(db, user.id)
    state = phone.challenge_state(ch, now)
    if user.phone_fp is not None:
        view = "done"
    elif not phone.need_phone(db, s, user, now, getattr(st, "demo_fp", None)):
        view = "not_needed"
    elif link:
        view = "link"
    elif state == "pending":
        view = "pending"
    else:
        view = "start"
    minutes = int(phone.CHALLENGE_TTL.total_seconds() // 60)
    return st.render(
        request, "app_phone.html", status, view=view, link=link, error=error,
        heading=fill(t("ph_h"), n=phone.bonus_pages(s)), valid=fill(t("ph_valid"), n=minutes),
        outcome=t(OUTCOME_TEXT[state]) if view == "start" and state in OUTCOME_TEXT else "",
        **shell(request, db, user, "up", [("", t("ph_crumb"))]),
    )


@router.get("/app/phone", dependencies=[Depends(_enabled)])
def phone_get(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _touch(request, db, user)
    return phone_page(request, db, user)


@router.post("/app/phone", dependencies=[Depends(_enabled), Depends(csrf_protect)])
def phone_post(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    st, now = _st(request), _now(request)
    if user.phone_fp is not None or not phone.need_phone(db, st.settings, user, now, getattr(st, "demo_fp", None)):
        return phone_page(request, db, user)
    token = phone.create_challenge(db, user, _ipf(request), now)
    if token is None:
        return phone_page(request, db, user, error=_t(request)("ph_many"), status=429)
    link = f"https://t.me/{st.settings.bot_username}?start={proto.START_PREFIX}{token}"
    return phone_page(request, db, user, link=link)


# ---------- internal: the bot reports a confirmed number ----------

def _answer(status: str) -> JSONResponse:
    return JSONResponse({"status": status}, status_code=HTTP[status], headers={"Cache-Control": "no-store"})


def _parse(raw: bytes) -> tuple[str, str, int] | None:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    token, fp, ts = data.get("token"), data.get("phone_fp"), data.get("ts")
    if not (isinstance(token, str) and proto.TOKEN_RE.fullmatch(token) and isinstance(fp, str)
            and proto.FP_RE.fullmatch(fp) and isinstance(ts, int) and not isinstance(ts, bool)):
        return None
    return token, fp, ts


def _confirm(request: Request, raw: bytes, signature: str | None) -> str:
    st = _st(request)
    s, now = st.settings, st.clock()
    with st.sessionmaker() as db:
        if phone.limited_all(db, now):
            return proto.LIMITED
        parsed = _parse(raw)
        if parsed is None:
            return proto.DENIED
        token, fp, ts = parsed
        now_ts = now.replace(tzinfo=timezone.utc).timestamp()
        if not proto.signature_ok(s.phone_verify_secret, token, fp, ts, signature) or not proto.fresh(ts, now_ts):
            return proto.DENIED
        if phone.limited_token(db, token, now):
            return proto.LIMITED
        return phone.confirm(db, s, token, fp, now)


LOOPBACK = {"127.0.0.1", "::1"}
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "x-forwarded-host")


def _local_only(request: Request) -> None:
    """Second wall behind the proxy's `deny`: outside dev only a caller on this machine (the bot, over localhost)
    reaches the endpoint. With uvicorn --proxy-headers a request that came through nginx carries the visitor's
    address here, never the loopback one, so a forgotten nginx rule does not open the endpoint. Same 404 as an
    unknown address."""
    if _st(request).settings.dev:
        return
    if request.client is None or request.client.host not in LOOPBACK:
        raise HTTPException(status_code=404)
    # The bot calls us directly and sends no proxy headers; anything that came through nginx carries them. So even a
    # wrong real-IP setup in nginx (a header that makes $remote_addr 127.0.0.1) does not open this endpoint.
    if any(h in request.headers for h in PROXY_HEADERS):
        raise HTTPException(status_code=404)


@router.post("/internal/phone-verified", dependencies=[Depends(_enabled), Depends(_local_only)])
async def phone_verified(request: Request):
    raw = await request.body()  # at most 8 KB: every POST except the upload is size-checked (web/main.py)
    result = await run_in_threadpool(_confirm, request, raw, request.headers.get(proto.SIGNATURE_HEADER))
    log.info("phone confirmation: %s", result)  # the outcome word only
    return _answer(result)
