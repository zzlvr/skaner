"""Skaner website: FastAPI app factory, pages, language switch, login, security headers."""

import hmac
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from itsdangerous import BadSignature, TimestampSigner, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from . import auth
from . import payments
from . import security as sec
from .admin import router as admin_router
from .cabinet import purge_results, router as cabinet_router
from .company_routes import router as company_router
from .contacts import router as contacts_router
from .config import LANGS, Settings
from .db import ensure_schema, make_engine, make_sessionmaker
from .deps import (
    CSRF_COOKIE, LOGIN_COOKIE, SESSION_COOKIE, AccountBlocked, CsrfFailed, LoginRequired, ReauthRequired, csrf_protect,
    current_user, get_db, require_user,
)
from .i18n import pick_lang, translator
from .alerts import Alerter
from .jobs import Context, MemoryBudget, UploadGate, mark_stale
from .mailer import Mailer, make_mailer
from .models import User
from .turnstile import Turnstile, make_turnstile

log = logging.getLogger("skaner.web")
HERE = Path(__file__).parent
LANG_COOKIE = "lang"

# Strict by default: only our own files, no inline scripts, no framing.
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; font-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

# The login page with the Turnstile widget (Cloudflare documents exactly these two sources for the widget).
CSP_CAPTCHA = CSP.replace("script-src 'self'", "script-src 'self' https://challenges.cloudflare.com") +     "; frame-src https://challenges.cloudflare.com"

# Pages that are not written yet; they show a "coming soon" stub, never a 404.
SOURCE_URL = "https://github.com/zzlvr/skaner"
LANG_NAMES = (("en", "English"), ("ru", "Русский"), ("kk", "Қазақша"))
STUB_PAGES = ("bot",)
LEGAL_PAGES = {"privacy": "legal_privacy.html", "terms": "legal_terms.html", "security": "legal_security.html"}

NO_STORE = ("/login", "/lang", "/account", "/logout", "/app", "/admin", "/internal", "/join", "/contacts")
# Every POST carries a few short fields only (the longest: a payment request, about 5 KB in the worst case), except
# the upload, which has its own streaming parser and limits (web/upload.py). Checked for every POST path, also
# unknown ones, so a form route that is not listed anywhere cannot be fed a huge body (or files Starlette would spool
# to disk), and the answer to a big body does not tell which paths exist.
LARGE_BODY_PATHS = ("/app/upload",)
MAX_SMALL_FORM = 8 * 1024
# Hidden areas: anyone who may not use them gets the same plain 404 as an unknown address, for every method.
HIDDEN = ("/admin", "/internal")
LOGIN_COOKIE_AGE = 15 * 60
SESSION_COOKIE_AGE = int(auth.SESSION_TTL.total_seconds())
CSRF_COOKIE_AGE = 365 * 24 * 3600


def safe_next(value: str) -> str:
    """Only local absolute paths: blocks open redirects such as //evil.com, https://evil.com or /\\t/evil.com.

    Printable ASCII only: browsers drop tabs and line breaks from URLs, which would turn "/\\t/x" into "//x".
    """
    if (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and all(0x21 <= ord(c) < 0x7F for c in value)
        and "\\" not in value
    ):
        return value
    return "/"


def after_login(value: str) -> str:
    n = safe_next(value)
    return n if n != "/" else "/account"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def default_backend_factory(settings: Settings) -> Callable[[], object]:
    """A new recognition backend per batch (so its cost is the batch's cost). Always the paid Claude API,
    never DOCSTRUC_BACKEND: client documents must not go to a free tier. Needs ANTHROPIC_API_KEY when called."""

    def make():
        from docstruc.llm import get_backend

        return get_backend("claude", model=settings.model)

    return make


def create_app(
    settings: Settings | None = None, *, mailer: Mailer | None = None, clock: Callable[[], datetime] | None = None,
    backend_factory: Callable[[], object] | None = None, executor=None, notifier=None,
    turnstile: Turnstile | None = None,
) -> FastAPI:
    """`mailer`, `clock`, `backend_factory`, `executor` and `notifier` are injection points for tests; by default
    the configured mailer, real UTC time, the Claude backend, a pool of WEB_WORKERS background threads and the
    Telegram notice to the owner (or none when TELEGRAM_BOT_TOKEN / BOT_OWNER_ID are not set)."""
    settings = settings or Settings.load()
    mailer = mailer or make_mailer(settings)  # production without a mail key fails here, at startup
    clock = clock or utcnow
    engine = make_engine(settings.db_url)
    ensure_schema(engine, settings.db_url, settings.dev)
    executor = executor or ThreadPoolExecutor(max_workers=max(1, settings.workers), thread_name_prefix="batch")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    # Local development only: one demo address with a permanent code. Never in production.
    demo_fp = None
    if settings.demo_email or settings.demo_code:
        if not settings.dev:
            raise RuntimeError("the demo login works only in local development (WEB_DEV=1)")
        demo_normalized = sec.normalize_email(settings.demo_email)
        if demo_normalized is None or not re.fullmatch(r"[0-9]{6}", settings.demo_code):
            raise RuntimeError("WEB_DEMO_EMAIL must be a valid address and WEB_DEMO_CODE exactly six digits")
        demo_fp = sec.email_fp(settings, demo_normalized)

    # redirect_slashes=False: "/admin/" must not answer with a redirect to "/admin" (that would show the area exists);
    # a path with an extra slash is simply unknown (404), like any other wrong address.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan, redirect_slashes=False)
    app.state.settings = settings
    app.state.clock = clock
    app.state.mailer = mailer
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.executor = executor
    app.state.memory = MemoryBudget(settings.memory_total, settings.memory_user)
    app.state.uploads = UploadGate(settings.workers)  # decoding while checking uploads: bounded (web/jobs.py)
    app.state.notifier = notifier or payments.make_notifier(settings)
    app.state.alerter = Alerter(app.state.notifier)
    app.state.jobs = Context(settings, app.state.sessionmaker, clock, backend_factory or default_backend_factory(settings),
                             app.state.alerter)
    app.state.lang_names = LANG_NAMES
    app.state.turnstile = turnstile or make_turnstile(settings)  # None = no bot check (dev, tests)
    app.state.payments = payments.ManualPayment(settings, app.state.notifier, app.state.sessionmaker)
    # Admins: fingerprints of WEB_ADMIN_EMAILS, computed once here. The raw addresses are not kept anywhere else.
    admin_fps = set()
    for raw in settings.admin_emails:
        normalized = sec.normalize_email(raw)
        if normalized is None:
            raise RuntimeError("WEB_ADMIN_EMAILS contains an invalid address")
        admin_fps.add(sec.email_fp(settings, normalized))
    app.state.admin_fps = frozenset(admin_fps)
    mark_stale(app.state.jobs)  # jobs of a previous process cannot finish: failed, not charged
    with app.state.sessionmaker() as db:
        purge_results(db, clock())  # retention also runs on every cabinet visit (payment requests too)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    class _ClockSigner(TimestampSigner):
        def get_timestamp(self) -> int:
            return int(clock().replace(tzinfo=timezone.utc).timestamp())

    login_ser = URLSafeTimedSerializer(settings.key("cookie"), salt="pre-login", signer=_ClockSigner)

    def set_cookie(response: Response, name: str, value: str, max_age: int, path: str = "/") -> None:
        response.set_cookie(
            name, value, max_age=max_age, path=path, httponly=True, samesite="lax", secure=not settings.dev
        )

    def clear_cookie(response: Response, name: str, path: str = "/") -> None:
        response.delete_cookie(name, path=path, httponly=True, samesite="lax", secure=not settings.dev)

    def rotate_csrf(request: Request, response: Response) -> None:
        set_cookie(response, CSRF_COOKIE, sec.new_token(), CSRF_COOKIE_AGE)
        request.state.csrf_rotated = True

    def oversize(request: Request) -> Response | None:
        if request.method != "POST" or request.url.path in LARGE_BODY_PATHS:
            return None
        length = request.headers.get("content-length")
        if length is None:
            return PlainTextResponse("Length Required", status_code=411)
        if not length.isdigit() or int(length) > MAX_SMALL_FORM:
            return PlainTextResponse("Payload Too Large", status_code=413)
        return None

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        st = request.state
        cid = request.cookies.get(CSRF_COOKIE)
        st.csrf_new = not sec.valid_token_shape(cid)
        st.csrf_id = sec.new_token() if st.csrf_new else cid
        st.csrf_used = False
        response = oversize(request) or await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = CSP_CAPTCHA if request.url.path == "/login" and app.state.turnstile else CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        # The invitation link carries its token in the path: pages under /join/ send no Referer at all (not even to
        # our own static files, whose requests a proxy log would record with the Referer).
        h["Referrer-Policy"] = "no-referrer" if request.url.path.startswith("/join/") else \
            "strict-origin-when-cross-origin"
        h["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
        if not settings.dev:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith(NO_STORE):
            h["Cache-Control"] = "no-store"
        if request.url.path.endswith((".css", ".js")) and request.url.path.startswith("/static/"):
            h["Cache-Control"] = "no-cache"  # the browser keeps a copy but asks the server first (a cheap 304)
        if request.url.path.startswith("/admin"):
            h["X-Robots-Tag"] = "noindex, nofollow"
        # Set the CSRF cookie only on pages that rendered a token: parallel static requests must not race it.
        if st.csrf_new and st.csrf_used and not getattr(st, "csrf_rotated", False):
            set_cookie(response, CSRF_COOKIE, st.csrf_id, CSRF_COOKIE_AGE)
        refresh = getattr(st, "session_refresh", None)
        if refresh and not getattr(st, "session_changed", False):
            set_cookie(response, SESSION_COOKIE, refresh[0], refresh[1])
        elif getattr(st, "session_clear", False) and not getattr(st, "session_changed", False):
            clear_cookie(response, SESSION_COOKIE)
        return response

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        # The route pattern (never the address itself: it can hold an invitation token) and the class name only.
        route = getattr(request.scope.get("route"), "path", "?")
        await run_in_threadpool(app.state.alerter.site_error, exc, route)
        log.error("unhandled error: %s", type(exc).__name__)
        return PlainTextResponse("Internal Server Error", status_code=500)

    @app.exception_handler(CsrfFailed)
    async def _csrf_failed(request: Request, exc: CsrfFailed):
        return PlainTextResponse("Forbidden", status_code=403)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        # /admin and /internal answer 404 for everything a stranger could probe, including "method not allowed".
        status = 404 if request.url.path.startswith(HIDDEN) and exc.status_code == 405 else exc.status_code
        if status == 404:
            # A browser opening an address gets the site's own page; everything else keeps the plain JSON. Hidden areas
            # (/admin, /internal) and unknown addresses go through this same code, so nothing tells them apart.
            if request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", ""):
                return render(request, "notfound.html", 404, user=None, lang_next="/")
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return JSONResponse({"detail": exc.detail}, status_code=status, headers=getattr(exc, "headers", None))

    @app.exception_handler(ReauthRequired)
    async def _reauth(request: Request, exc: ReauthRequired):
        return RedirectResponse("/login?next=/admin&reauth=1", status_code=303)

    @app.exception_handler(AccountBlocked)
    async def _blocked(request: Request, exc: AccountBlocked):
        return render(request, "blocked.html", 403)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login?next=" + quote(safe_next(exc.path), safe="/"), status_code=303)

    def lang_of(request: Request) -> str:
        return pick_lang(request.cookies.get(LANG_COOKIE), request.headers.get("accept-language"))

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        lang = lang_of(request)
        request.state.csrf_used = True
        context = {
            "paid_plans": settings.paid_plans, "source_url": SOURCE_URL, "lang": lang, "langs": LANGS, "lang_names": LANG_NAMES, "t": translator(lang),
            "csrf_token": sec.csrf_token(settings, request.state.csrf_id),
            "turnstile_site_key": settings.turnstile_site_key if app.state.turnstile else "", **ctx,
        }
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def client_ip(request: Request) -> str | None:
        # Behind nginx/Cloudflare this is correct only with uvicorn --proxy-headers --forwarded-allow-ips=<proxy>.
        return request.client.host if request.client else None

    app.state.render = render
    app.state.lang_of = lang_of
    app.state.client_ip = client_ip
    app.state.clear_cookie = clear_cookie
    app.state.rotate_csrf = rotate_csrf

    def read_login_cookie(request: Request) -> dict | None:
        raw = request.cookies.get(LOGIN_COOKIE)
        if not raw or len(raw) > 512:
            return None
        try:
            data = login_ser.loads(raw, max_age=LOGIN_COOKIE_AGE)
        except BadSignature:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("fp"), str) or len(data["fp"]) != 64:
            return None
        return {"fp": data["fp"], "next": after_login(data.get("n", ""))}

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    @app.get("/")
    def index(request: Request, user: User | None = Depends(current_user)):
        return render(request, "index.html", user=user)

    @app.post("/lang", dependencies=[Depends(csrf_protect)])
    def set_lang(
        request: Request, lang: str = Form(...), next: str = Form("/"),
        user: User | None = Depends(current_user), db: Session = Depends(get_db),
    ):
        response = RedirectResponse(safe_next(next), status_code=303)
        if lang in LANGS:
            set_cookie(response, LANG_COOKIE, lang, 365 * 24 * 3600)
            if user is not None:
                user.lang = lang  # same request-scoped db session as current_user
                db.commit()
        return response

    # ---------- login by one-time email code ----------

    @app.get("/login")
    def login_page(request: Request, next: str = "", reauth: str = "", user: User | None = Depends(current_user)):
        # reauth=1: signed in, but a page (the admin) wants a fresh sign-in; the new login replaces the session.
        if user is not None and reauth != "1":
            return RedirectResponse(after_login(next), status_code=303)
        return render(request, "login.html", next=safe_next(next) if next else "")

    @app.post("/login", dependencies=[Depends(csrf_protect)])
    def login_submit(
        request: Request, background: BackgroundTasks, email: str = Form(""), next: str = Form(""),
        captcha: str = Form("", alias="cf-turnstile-response"), db: Session = Depends(get_db),
    ):
        t, now = translator(lang_of(request)), clock()
        ipf = sec.ip_fp(settings, client_ip(request))
        if auth.hit_ip_limit(db, auth.BUCKET_CODE_REQ, ipf, auth.IP_REQUESTS_PER_HOUR, now):
            return render(request, "login.html", 429, error=t("too_many"), next=safe_next(next) if next else "")
        auth.record(db, auth.BUCKET_CODE_REQ, ipf, now)
        if app.state.turnstile is not None and not app.state.turnstile.check(captcha, client_ip(request)):
            return render(request, "login.html", 400, email=email[:254], error=t("captcha_failed"),
                          next=safe_next(next) if next else "")
        normalized = sec.normalize_email(email)
        if normalized is None:
            return render(request, "login.html", 400, email=email[:254], error=t("email_invalid"),
                          next=safe_next(next) if next else "")
        fp = sec.email_fp(settings, normalized)
        auth.purge_expired(db, now)
        if auth.site_mail_capped(db, settings, now):  # same for every address, so it tells nothing about any of them
            if auth.first_cap_alert(db, now):
                background.add_task(app.state.notifier.send, "Skaner: достигнут дневной или часовой лимит писем с кодами "
                                    "входа. Новые коды не отправляются, пока лимит не освободится. Проверьте /admin.")
            return render(request, "login.html", 503, email=email[:254], error=t("mail_busy"),
                          next=safe_next(next) if next else "")
        code = auth.issue_code(db, settings, fp, ipf, now)
        if code is not None:  # sent after the response: same timing whether sent, throttled or failed
            background.add_task(auth.send_code_mail, mailer, normalized, lang_of(request), code, app.state.alerter)
        # Identical response for new, known and throttled addresses.
        response = RedirectResponse("/login/code", status_code=303)
        set_cookie(response, LOGIN_COOKIE, login_ser.dumps({"fp": fp, "n": after_login(next)}),
                   LOGIN_COOKIE_AGE, path="/login")
        return response

    @app.get("/login/code")
    def login_code_page(request: Request):
        if read_login_cookie(request) is None:
            return RedirectResponse("/login", status_code=303)
        return render(request, "login_code.html")

    @app.post("/login/code", dependencies=[Depends(csrf_protect)])
    def login_code_submit(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
        pending = read_login_cookie(request)
        if pending is None:
            return RedirectResponse("/login", status_code=303)
        t, now = translator(lang_of(request)), clock()
        ipf = sec.ip_fp(settings, client_ip(request))
        if auth.ip_fails_exceeded(db, ipf, now):
            return render(request, "login_code.html", 429, error=t("too_many"))
        cleaned = sec.clean_code(code)
        is_demo = (
            demo_fp is not None and cleaned is not None
            and hmac.compare_digest(pending["fp"], demo_fp) and hmac.compare_digest(cleaned, settings.demo_code)
        )
        if cleaned is None or not (is_demo or auth.verify_code(db, settings, pending["fp"], cleaned, now)):
            if cleaned is not None:
                auth.record(db, auth.BUCKET_CODE_FAIL, ipf, now)
            return render(request, "login_code.html", 400, error=t("code_invalid"))
        user = auth.get_or_create_user(db, pending["fp"], lang_of(request), now)
        if is_demo:
            auth.refresh_demo_trial(db, settings, user, now)
        if user.blocked_at is not None:  # the code was right, but the owner blocked this account: no session
            response = render(request, "blocked.html", 403)
            clear_cookie(response, LOGIN_COOKIE, path="/login")
            return response
        auth.revoke_session(db, request.cookies.get(SESSION_COOKIE))  # no session fixation
        token = auth.create_session(db, user.id, ipf, sec.ua_fp(settings, request.headers.get("user-agent")), now)
        response = RedirectResponse(pending["next"], status_code=303)
        set_cookie(response, SESSION_COOKIE, token, SESSION_COOKIE_AGE)
        clear_cookie(response, LOGIN_COOKIE, path="/login")
        rotate_csrf(request, response)
        request.state.session_changed = True
        return response

    @app.post("/logout", dependencies=[Depends(csrf_protect)])
    def logout(request: Request, db: Session = Depends(get_db)):
        auth.revoke_session(db, request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/", status_code=303)
        clear_cookie(response, SESSION_COOKIE)
        rotate_csrf(request, response)
        request.state.session_changed = True
        return response

    def stub(request: Request, user: User | None = Depends(current_user)):
        return render(request, "soon.html", user=user)

    for slug in STUB_PAGES:
        app.add_api_route(f"/{slug}", stub, methods=["GET"], name=slug)

    def legal_page(template: str):
        def page(request: Request, user: User | None = Depends(current_user)):
            return render(request, template, user=user)
        return page

    for slug, template in LEGAL_PAGES.items():
        app.add_api_route(f"/{slug}", legal_page(template), methods=["GET"], name=slug)

    app.include_router(contacts_router)  # /contacts: write to the owner (signed-in visitors)
    app.include_router(cabinet_router)  # /account, /app/... (upload, results, downloads, history, plans, account)
    app.include_router(company_router)  # /app/company and /join/<token> (company cabinets, web/company.py)
    app.include_router(admin_router)  # /admin/... (owner only; 404 for everyone else)
    return app
