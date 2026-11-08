"""Reusable FastAPI dependencies: database session, current user, login requirement, CSRF check."""

from urllib.parse import urlsplit

from fastapi import Depends, Form, Request
from sqlalchemy.orm import Session

from . import auth
from . import security as sec
from .models import User

SESSION_COOKIE = "sk_session"
CSRF_COOKIE = "sk_csrf"
LOGIN_COOKIE = "sk_login"  # pre-login: signed email fingerprint only, never the address


class LoginRequired(Exception):
    def __init__(self, path: str):
        self.path = path


class CsrfFailed(Exception):
    pass


class ReauthRequired(Exception):
    """Admin pages need a sign-in from the last hours: send the owner to the login page again."""


class AccountBlocked(Exception):
    """The owner blocked this account: no cabinet, no uploads (web/main.py renders a short message)."""


def get_db(request: Request):
    db = request.app.state.sessionmaker()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The signed-in user or None. Renews a sliding session at most once per hour."""
    st = request.state
    if hasattr(st, "user"):
        return st.user
    st.user = None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    now = request.app.state.clock()
    found = auth.load_session(db, token, now)
    if found is None:
        st.session_clear = True  # stale or forged cookie: drop it
        return None
    session, user, renewed = found
    if renewed:
        st.session_refresh = (token, int((session.expires_at - now).total_seconds()))
    st.user = user
    st.session_created_at = session.created_at  # admin pages need a fresh sign-in
    return user


def _guest(request: Request, db: Session) -> User | None:
    """Free mode needs no sign-in: a visitor who opens a page gets a guest account and a session. Only for page
    views (GET), and only a limited number per network per day; otherwise the normal sign-in page."""
    st = request.app.state
    settings = st.settings
    if settings.paid_plans or request.method != "GET":
        return None
    now = st.clock()
    ipf = sec.ip_fp(settings, st.client_ip(request))
    if (auth.guest_limit_hit(db, ipf, settings.guests_per_ip_day, now)
            or auth.guest_limit_hit(db, "site", settings.guests_per_day, now)):
        return None
    guest = auth.create_guest(db, st.lang_of(request), now)
    auth.record(db, auth.BUCKET_GUEST, ipf, now)
    auth.record(db, auth.BUCKET_GUEST, "site", now)
    token = auth.create_session(db, guest.id, ipf, sec.ua_fp(settings, request.headers.get("user-agent")), now)
    request.state.user = guest
    request.state.session_refresh = (token, int(auth.SESSION_TTL.total_seconds()))
    request.state.session_created_at = now
    return guest


def require_user(request: Request, user: User | None = Depends(current_user), db: Session = Depends(get_db)) -> User:
    if user is None:
        user = _guest(request, db)
        if user is None:
            raise LoginRequired(request.url.path)
    if user.blocked_at is not None:
        raise AccountBlocked()
    return user


def _origin(url: str) -> str | None:
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    return f"{p.scheme}://{p.netloc}".lower() if p.scheme and p.netloc else None


def origin_ok(request: Request) -> bool:
    """Origin (or Referer when Origin is absent) must be our own site. Neither header: rely on the token."""
    settings = request.app.state.settings
    allowed = {_origin(settings.base_url)}
    if settings.dev:  # dev server may be opened as localhost or 127.0.0.1
        allowed.add(f"{request.url.scheme}://{request.headers.get('host', '')}".lower())
    src = request.headers.get("origin")
    if src is None:
        referer = request.headers.get("referer")
        if not referer:
            return True
        src = _origin(referer)
    return src is not None and src.lower() in allowed


def csrf_protect(request: Request, csrf_token: str = Form("")) -> None:
    """Signed double-submit token (HMAC of the per-browser sk_csrf cookie) plus an Origin/Referer check."""
    settings = request.app.state.settings
    if not origin_ok(request) or not sec.csrf_ok(settings, request.cookies.get(CSRF_COOKIE), csrf_token):
        raise CsrfFailed()
