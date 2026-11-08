"""Payment step, manual for now: "I want to pay" stores a request and tells the owner in Telegram;
the owner switches the plan on by hand in /admin.

The rest of the site talks to this module only through `PaymentProvider.submit(...)`. When automatic payment comes
(after the sole proprietorship is registered), a new provider replaces ManualPayment; pages and plans stay.

Privacy: company, contact and comment live only inside the encrypted blob of the request
(web/crypto.encrypt_request). Logs carry counts and exception class names only. The Telegram text has no email
(the site has none; addresses typed into the form are refused) and is sent as plain text: no parse_mode, so no
markup can be injected; every value is cleaned and cut to its limit.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import httpx
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from . import crypto
from .accounts import PLAN_NAMES, PLANS
from .config import Settings
from .models import PaymentRequest, RateEvent, User

log = logging.getLogger("skaner.web.payments")

TIMEOUT = 5.0
COMPANY_MAX, CONTACT_MIN, CONTACT_MAX, COMMENT_MAX = 120, 3, 80, 500
MESSAGE_MIN, MESSAGE_MAX = 5, 1000  # a message to the owner from the Contacts page (stored like a payment request)
USER_PER_DAY, IP_PER_DAY = 3, 10
BUCKET_USER, BUCKET_IP = "pay_user", "pay_ip"
KEEP_DONE, KEEP_OPEN = timedelta(days=90), timedelta(days=365)
DAY = timedelta(days=1)

# "Something is wrong with this result" from the batch page: stored like a message (plan="feedback"), encrypted, with
# its own daily limits. The document itself is never sent: the owner asks for it only if the user wants help.
FEEDBACK_KINDS = {
    "amount": "wrong amount or VAT", "date": "wrong date", "id": "wrong BIN/IIN or document number",
    "lines": "a line is missing or extra", "names": "wrong name or counterparty",
    "export": "problem with the file (Excel, 1C, CSV, PDF)", "other": "other",
}
FEEDBACK_MAX = 500
FB_USER_PER_DAY, FB_IP_PER_DAY = 10, 30
BUCKET_FB_USER, BUCKET_FB_IP = "fb_user", "fb_ip"

# Control, format (zero-width, bidi overrides), surrogate, private-use, unassigned and line/paragraph separators.
_BAD = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")
EMAIL_LIKE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


# ---------- form ----------

@dataclass
class RequestForm:
    plan: str
    company: str
    contact: str
    comment: str


class FormError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code  # plan, contact, text, long, email


def _clean(raw: str, limit: int, multiline: bool = False) -> str:
    """NFKC, trimmed; line breaks allowed only where `multiline`. Raises FormError for bad characters or length."""
    if not isinstance(raw, str) or len(raw) > 4 * limit:
        raise FormError("long")
    s = unicodedata.normalize("NFKC", raw).replace("\r\n", "\n").strip()
    for c in s:
        if c == "\n" and multiline:
            continue
        if unicodedata.category(c) in _BAD:
            raise FormError("text")
    if len(s) > limit:
        raise FormError("long")
    if EMAIL_LIKE.search(s):
        raise FormError("email")
    return s


def parse_form(plan: str, company: str, contact: str, comment: str) -> RequestForm:
    if plan not in PLANS:
        raise FormError("plan")
    company = _clean(company, COMPANY_MAX)
    comment = _clean(comment, COMMENT_MAX, multiline=True)
    try:
        contact = _clean(contact, CONTACT_MAX)
    except FormError as e:
        raise FormError("contact" if e.code == "long" else e.code) from None
    if len(contact) < CONTACT_MIN:
        raise FormError("contact")
    return RequestForm(plan, company, contact, comment)


def parse_question(contact: str, message: str) -> RequestForm:
    """A message to the owner: same cleaning as a payment request, stored with plan="question"."""
    try:
        contact = _clean(contact, CONTACT_MAX)
    except FormError as e:
        raise FormError("contact" if e.code == "long" else e.code) from None
    if len(contact) < CONTACT_MIN:
        raise FormError("contact")
    message = _clean(message, MESSAGE_MAX, multiline=True)
    if len(message) < MESSAGE_MIN:
        raise FormError("short")
    return RequestForm("question", "", contact, message)


def parse_feedback(kind: str, comment: str) -> tuple[str, str]:
    """(kind, cleaned comment); the comment is optional. Raises FormError("kind") or the cleaning errors."""
    if kind not in FEEDBACK_KINDS:
        raise FormError("kind")
    return kind, _clean(comment, FEEDBACK_MAX, multiline=True)


# ---------- limits ----------

def _count(db: Session, bucket: str, key: str, now: datetime) -> int:
    return db.scalar(select(func.count()).select_from(RateEvent).where(
        RateEvent.bucket == bucket, RateEvent.key_fp == key, RateEvent.created_at > now - DAY)) or 0


def over_limit(db: Session, user_id: int, ip_fp: str, now: datetime) -> bool:
    return (_count(db, BUCKET_USER, f"user:{user_id}", now) >= USER_PER_DAY
            or _count(db, BUCKET_IP, ip_fp, now) >= IP_PER_DAY)


# ---------- owner notification ----------

class Notifier(Protocol):
    def send(self, text: str) -> bool: ...


class NullNotifier:
    """Telegram not configured: the request is only stored (it shows in /admin/requests)."""

    configured = False

    def send(self, text: str) -> bool:
        return False


class _NoTokenInLogs(logging.Filter):
    """httpx logs request URLs at INFO, and a Telegram URL contains the bot token. Drop such records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "api.telegram.org" not in record.getMessage()
        except Exception:  # noqa: BLE001
            return False


_httpx_log = logging.getLogger("httpx")
_httpx_log.setLevel(logging.WARNING)  # same as bot/main.py
if not any(isinstance(f, _NoTokenInLogs) for f in _httpx_log.filters):
    _httpx_log.addFilter(_NoTokenInLogs())


class TelegramNotifier:
    """Bot API sendMessage to the owner. One attempt, 5 s timeout, no retries (a retry could duplicate the notice),
    no redirects. Never logs the text, the token or the response body."""

    configured = True
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str, transport: httpx.BaseTransport | None = None):
        if not token or not chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and BOT_OWNER_ID must both be set")
        self._token, self._chat = token, chat_id
        self._client = httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=False)

    def __repr__(self) -> str:
        return "TelegramNotifier(<hidden>)"

    def send(self, text: str) -> bool:
        try:
            r = self._client.post(f"{self.API}/bot{self._token}/sendMessage", json={
                "chat_id": self._chat, "text": text[:4000], "link_preview_options": {"is_disabled": True},
            })
        except httpx.HTTPError as exc:
            log.warning("owner notice not sent: %s", type(exc).__name__)
            return False
        if not 200 <= r.status_code < 300:
            log.warning("owner notice not sent: status %d", r.status_code)
            return False
        return True


def make_notifier(settings: Settings) -> Notifier:
    if settings.telegram_token and settings.telegram_owner_id:
        return TelegramNotifier(settings.telegram_token, settings.telegram_owner_id)
    return NullNotifier()


def _line(value: str, limit: int) -> str:
    """One plain line for the notice: no line breaks (they could fake extra lines), cut to the limit."""
    s = " ".join(value.split())
    return (s[:limit] + "…") if len(s) > limit else s


def notice_text(code: str, form: RequestForm) -> str:
    if form.plan == "question":
        return "\n".join([
            "Skaner: message from the site",
            f"Account: {code}",
            f"Contact: {_line(form.contact, CONTACT_MAX)}",
            f"Message: {_line(form.comment, MESSAGE_MAX)}",
            "admin: /admin/requests",
        ])
    plan = PLANS[form.plan]
    return "\n".join([
        "Skaner: payment request",
        f"Account: {code}",
        f"Plan: {PLAN_NAMES[plan.key]} ({plan.quota} pages, {plan.price} ₸/month)",
        f"Company: {_line(form.company, COMPANY_MAX) or '—'}",
        f"Contact: {_line(form.contact, CONTACT_MAX)}",
        f"Comment: {_line(form.comment, COMMENT_MAX) or '—'}",
        "admin: /admin/requests",
    ])


# ---------- the payment step ----------

class PaymentProvider(Protocol):
    def submit(self, db: Session, user: User, form: RequestForm, ip_fp: str, now: datetime) -> int: ...


def feedback_over_limit(db: Session, user_id: int, ip_fp: str, now: datetime) -> bool:
    return (_count(db, BUCKET_FB_USER, f"user:{user_id}", now) >= FB_USER_PER_DAY
            or _count(db, BUCKET_FB_IP, ip_fp, now) >= FB_IP_PER_DAY)


def feedback_notice_text(code: str, kind: str, batch_ref: str, doctype: str, fmt: str, comment: str) -> str:
    return "\n".join([
        "Skaner: problem with a result",
        f"Account: {code}",
        f"What is wrong: {FEEDBACK_KINDS[kind]}",
        f"Upload: {batch_ref} ({_line(doctype or '-', 20)}, {_line(fmt, 12)})",
        f"Comment: {_line(comment, FEEDBACK_MAX) or '—'}",
        f"admin: /admin/users/{code}",
    ])


class ManualPayment:
    """Store the encrypted request and count it against the limits. The Telegram notice is sent afterwards
    (`notify`, run after the response) so the user sees the same page whether it worked or not."""

    def __init__(self, settings: Settings, notifier: Notifier, sessionmaker):
        self.settings, self.notifier, self.sessionmaker = settings, notifier, sessionmaker

    def submit(self, db: Session, user: User, form: RequestForm, ip_fp: str, now: datetime) -> int:
        row = PaymentRequest(user_id=user.id, plan=form.plan, status="new", created_at=now)
        db.add(row)
        db.flush()  # assigns row.id, which the blob is bound to
        row.data_enc = crypto.encrypt_request(self.settings, row.id, {
            "v": 1, "plan": form.plan, "company": form.company, "contact": form.contact, "comment": form.comment,
        })
        db.add(RateEvent(bucket=BUCKET_USER, key_fp=f"user:{user.id}", created_at=now))
        db.add(RateEvent(bucket=BUCKET_IP, key_fp=ip_fp, created_at=now))
        db.commit()
        log.info("payment request stored")
        return row.id

    def submit_feedback(self, db: Session, user: User, kind: str, comment: str, batch_ref: str, doctype: str,
                        fmt: str, ip_fp: str, now: datetime) -> int:
        row = PaymentRequest(user_id=user.id, plan="feedback", status="new", created_at=now)
        db.add(row)
        db.flush()
        row.data_enc = crypto.encrypt_request(self.settings, row.id, {
            "v": 1, "plan": "feedback", "kind": kind, "batch": batch_ref, "doctype": doctype, "fmt": fmt,
            "comment": comment,
        })
        db.add(RateEvent(bucket=BUCKET_FB_USER, key_fp=f"user:{user.id}", created_at=now))
        db.add(RateEvent(bucket=BUCKET_FB_IP, key_fp=ip_fp, created_at=now))
        db.commit()
        log.info("result feedback stored")
        return row.id

    def notify(self, request_id: int, text: str, now: datetime) -> None:
        try:
            ok = self.notifier.send(text)
        except Exception as exc:  # noqa: BLE001  (a notifier bug must not hide the request)
            log.warning("owner notice failed: %s", type(exc).__name__)
            ok = False
        if ok:
            with self.sessionmaker() as db:
                row = db.get(PaymentRequest, request_id)
                if row is not None:
                    row.notified_at = now
                    db.commit()


def read(settings: Settings, row: PaymentRequest) -> dict | None:
    """Decrypted fields for the admin, or None when the blob is unreadable."""
    try:
        return crypto.decrypt_request(settings, row.id, row.data_enc)
    except crypto.ResultsUnreadable:
        log.warning("payment request could not be decrypted")
        return None


def purge_requests(db: Session, now: datetime) -> int:
    """Retention: 90 days after being marked done, 365 days if never handled."""
    n = db.execute(delete(PaymentRequest).where(or_(
        and_(PaymentRequest.status == "done", PaymentRequest.done_at < now - KEEP_DONE),
        and_(PaymentRequest.status != "done", PaymentRequest.created_at < now - KEEP_OPEN),
    ))).rowcount
    db.commit()
    if n:
        log.info("old payment requests purged: %d", n)
    return n
