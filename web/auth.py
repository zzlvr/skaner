"""Passwordless login: one-time email codes, rate limits, sessions. All state lives in the database.

Nothing here sees or stores a raw email except `send_code_mail` (in memory, to deliver the code).
"""

import hmac
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import security as sec
from .accounts import assign_code, delete_user, new_account_code, trial_used_before
from .config import Settings
from .i18n import translator
from .models import Batch, LoginCode, RateEvent, User, UserSession

log = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5  # wrong tries per code, then the code is dead
RESEND_COOLDOWN = timedelta(seconds=60)
EMAIL_CODES_PER_HOUR = 5
EMAIL_CODES_PER_DAY = 10  # stricter than hourly alone: caps guessing on one address to 50 tries a day
IP_REQUESTS_PER_HOUR = 20
IP_FAILS_PER_HOUR = 30
SESSION_TTL = timedelta(days=30)  # sliding
SESSION_MAX = timedelta(days=90)  # absolute cap
SESSION_TOUCH = timedelta(hours=1)  # renew last_seen at most this often
HOUR, DAY = timedelta(hours=1), timedelta(days=1)

BUCKET_CODE_REQ = "code_req"
BUCKET_GUEST = "guest"
BUCKET_CODE_FAIL = "code_fail"
BUCKET_MAIL_ALERT = "mail_cap_alert"


# ---------- rate limits ----------

def _count(db: Session, bucket: str, key: str, since: datetime) -> int:
    q = select(func.count()).select_from(RateEvent).where(
        RateEvent.bucket == bucket, RateEvent.key_fp == key, RateEvent.created_at > since
    )
    return db.scalar(q) or 0


def hit_ip_limit(db: Session, bucket: str, ip: str, limit: int, now: datetime) -> bool:
    """True when the IP already reached `limit` events in the last hour. Over-limit hits are not stored."""
    return _count(db, bucket, ip, now - HOUR) >= limit


def record(db: Session, bucket: str, ip: str, now: datetime) -> None:
    db.add(RateEvent(bucket=bucket, key_fp=ip, created_at=now))
    db.commit()


def site_mail_capped(db: Session, settings: Settings, now: datetime) -> bool:
    """True when the whole site already sent its share of login mails (settings.mail_hourly / mail_daily)."""
    def sent(since: datetime) -> int:
        return db.scalar(select(func.count()).select_from(LoginCode).where(LoginCode.created_at > now - since)) or 0

    return sent(HOUR) >= settings.mail_hourly or sent(DAY) >= settings.mail_daily


def first_cap_alert(db: Session, now: datetime) -> bool:
    """True once per day: the moment to tell the owner that the mail cap was hit."""
    if _count(db, BUCKET_MAIL_ALERT, "site", now - DAY) > 0:
        return False
    record(db, BUCKET_MAIL_ALERT, "site", now)
    return True


def ip_fails_exceeded(db: Session, ip: str, now: datetime) -> bool:
    return hit_ip_limit(db, BUCKET_CODE_FAIL, ip, IP_FAILS_PER_HOUR, now)


# ---------- codes ----------

def issue_code(db: Session, settings: Settings, fp: str, ip: str, now: datetime) -> str | None:
    """Create a new code for this email fingerprint, or None when the address is throttled.

    Throttled requests create nothing: a silent extra row would kill the code the real owner just received.
    """
    last = db.scalar(select(func.max(LoginCode.created_at)).where(LoginCode.email_fp == fp))
    per_hour = db.scalar(
        select(func.count()).select_from(LoginCode).where(LoginCode.email_fp == fp, LoginCode.created_at > now - HOUR)
    )
    per_day = db.scalar(
        select(func.count()).select_from(LoginCode).where(LoginCode.email_fp == fp, LoginCode.created_at > now - DAY)
    )
    code = sec.new_code()  # always generated: same work on every path
    if (last and now - last < RESEND_COOLDOWN) or per_hour >= EMAIL_CODES_PER_HOUR or per_day >= EMAIL_CODES_PER_DAY:
        return None
    # Only the newest code is valid: older live codes for this address expire now.
    db.execute(
        update(LoginCode)
        .where(LoginCode.email_fp == fp, LoginCode.consumed_at.is_(None), LoginCode.expires_at > now)
        .values(expires_at=now)
    )
    row = LoginCode(email_fp=fp, code_hash="", created_at=now, expires_at=now + CODE_TTL, attempts=0, ip_fp=ip)
    db.add(row)
    db.flush()  # assigns row.id, which the hash is bound to
    row.code_hash = sec.code_hash(settings, row.id, fp, code)
    db.commit()
    return code


def verify_code(db: Session, settings: Settings, fp: str, code: str, now: datetime) -> bool:
    """Atomic check: an attempt is reserved (attempts+1) before comparing, so parallel guesses cannot exceed
    MAX_ATTEMPTS, and the code is consumed with a conditional UPDATE, so it works at most once."""
    row_id, stored = reserve_attempt(db, fp, now)
    expected = sec.code_hash(settings, row_id or 0, fp, code)  # computed on every path
    if row_id is None or not hmac.compare_digest(stored, expected):
        return False
    return consume(db, row_id, now)


def _live(now: datetime):
    return LoginCode.consumed_at.is_(None), LoginCode.expires_at > now


def reserve_attempt(db: Session, fp: str, now: datetime) -> tuple[int | None, str]:
    """Take one attempt on the newest live code (conditional UPDATE). Returns (row id, stored hash) or (None, "")."""
    row_id = db.scalar(
        select(LoginCode.id)
        .where(LoginCode.email_fp == fp, LoginCode.attempts < MAX_ATTEMPTS, *_live(now))
        .order_by(LoginCode.id.desc())
        .limit(1)
    )
    if row_id is None:
        return None, ""
    reserved = db.execute(
        update(LoginCode)
        .where(LoginCode.id == row_id, LoginCode.attempts < MAX_ATTEMPTS, *_live(now))
        .values(attempts=LoginCode.attempts + 1)
    )
    stored = db.scalar(select(LoginCode.code_hash).where(LoginCode.id == row_id))
    db.commit()
    return (row_id, stored or "") if reserved.rowcount == 1 else (None, "")


def consume(db: Session, row_id: int, now: datetime) -> bool:
    """Mark the code used. The WHERE makes it single-use even when two requests got this far in parallel."""
    done = db.execute(update(LoginCode).where(LoginCode.id == row_id, *_live(now)).values(consumed_at=now))
    db.commit()
    return done.rowcount == 1


def send_code_mail(mailer, to: str, lang: str, code: str, alerter=None) -> None:
    """Runs after the response is sent. Failures are logged without the address, code or provider reply."""
    t = translator(lang)
    try:
        mailer.send(to, t("mail_subject"), t("mail_body").replace("{code}", code))
    except Exception as exc:  # noqa: BLE001  (never let details reach the user or the log)
        log.warning("login mail not sent: %s", type(exc).__name__)
        if alerter is not None:
            alerter.mail_failed(exc)


# ---------- users and sessions ----------

def refresh_demo_trial(db: Session, settings: Settings, user: User, now: datetime) -> None:
    """Demo account only (local development): the trial never runs out, so the owner always sees a working cabinet."""
    user.trial_granted_at = user.trial_granted_at or now
    user.trial_pages_left = max(user.trial_pages_left, settings.trial_pages)
    user.trial_expires_at = max(user.trial_expires_at or now, now + timedelta(days=settings.trial_days))
    user.blocked_at = None
    db.commit()


def get_or_create_user(db: Session, fp: str, lang: str, now: datetime) -> User:
    """New users get an account code. An address whose earlier account was deleted after using the trial
    (a tombstone, web/accounts.py) starts with the trial already over: deleting does not restart it."""
    user = db.scalar(select(User).where(User.email_fp == fp))
    for _ in range(5):
        if user is not None:
            break
        new = User(email_fp=fp, lang=lang, created_at=now, account_code=new_account_code())
        if trial_used_before(db, fp):
            new.trial_granted_at, new.trial_pages_left, new.trial_expires_at = now, 0, now
        db.add(new)
        try:
            db.commit()
        except IntegrityError:  # created by a parallel request, or (very rarely) a duplicate account code
            db.rollback()
        user = db.scalar(select(User).where(User.email_fp == fp))
    if user is None:
        raise RuntimeError("could not create the user")
    assign_code(db, user)  # accounts created before codes existed (normally backfilled by migration 0004)
    user.last_login_at = now
    user.lang = lang
    db.commit()
    return user


def create_guest(db: Session, lang: str, now: datetime) -> User:
    """A guest account for a visitor who did not sign in: a random fingerprint instead of an email, a normal
    account code. It is deleted when its session is gone (purge_guests)."""
    for _ in range(5):
        guest = User(email_fp=secrets.token_hex(32), lang=lang, created_at=now, last_login_at=now,
                     account_code=new_account_code(), is_guest=True)
        db.add(guest)
        try:
            db.commit()
            return guest
        except IntegrityError:  # a duplicate account code (very rare)
            db.rollback()
    raise RuntimeError("could not create a guest")


def guest_limit_hit(db: Session, ip: str, limit: int, now: datetime) -> bool:
    return _count(db, BUCKET_GUEST, ip, now - DAY) >= limit


def merge_guest(db: Session, guest: User, user: User, now: datetime) -> None:
    """The visitor signed in after using the site as a guest: their uploads move to the account, the guest goes."""
    db.execute(update(Batch).where(Batch.user_id == guest.id).values(user_id=user.id))
    db.execute(update(Batch).where(Batch.created_by_user_id == guest.id).values(created_by_user_id=user.id))
    db.execute(update(Batch).where(Batch.wallet_user_id == guest.id).values(wallet_user_id=user.id))
    db.commit()
    delete_user(db, guest.id, now)


def purge_guests(db: Session, now: datetime) -> int:
    """Nightly: guests without a live session can never come back; delete them with their uploads."""
    live = select(UserSession.user_id).where(UserSession.expires_at > now)
    ids = db.scalars(select(User.id).where(User.is_guest.is_(True), User.id.not_in(live),
                                           User.created_at < now - timedelta(hours=1))).all()
    for user_id in ids:
        delete_user(db, user_id, now)
    return len(ids)


def create_session(db: Session, user_id: int, ip: str | None, ua: str | None, now: datetime) -> str:
    token = sec.new_token()
    db.add(UserSession(
        token_hash=sec.token_hash(token), user_id=user_id, created_at=now, expires_at=now + SESSION_TTL,
        last_seen_at=now, ip_fp=ip, user_agent_hash=ua,
    ))
    db.commit()
    return token


def load_session(db: Session, token: str | None, now: datetime) -> tuple[UserSession, User, bool] | None:
    """Returns (session, user, renewed) for a live session. `renewed` means the cookie should be re-sent."""
    if not sec.valid_token_shape(token):
        return None
    s = db.scalar(select(UserSession).where(UserSession.token_hash == sec.token_hash(token)))
    if s is None or s.expires_at <= now or s.created_at + SESSION_MAX <= now:
        return None
    user = db.get(User, s.user_id)
    if user is None:
        return None
    renewed = False
    if now - s.last_seen_at >= SESSION_TOUCH:
        s.last_seen_at = now
        s.expires_at = min(now + SESSION_TTL, s.created_at + SESSION_MAX)
        db.commit()
        renewed = True
    return s, user, renewed


def revoke_session(db: Session, token: str | None) -> None:
    if sec.valid_token_shape(token):
        db.execute(delete(UserSession).where(UserSession.token_hash == sec.token_hash(token)))
        db.commit()


def revoke_all_sessions(db: Session, user_id: int) -> int:
    """Sign the user out everywhere (e.g. after an account change). Returns the number of sessions removed."""
    n = db.execute(delete(UserSession).where(UserSession.user_id == user_id)).rowcount
    db.commit()
    return n


def purge_expired(db: Session, now: datetime) -> None:
    """Housekeeping: old codes and counters are kept a day (limits need them), dead sessions are removed."""
    db.execute(delete(LoginCode).where(LoginCode.created_at < now - DAY))
    db.execute(delete(RateEvent).where(RateEvent.created_at < now - DAY))
    db.execute(delete(UserSession).where(UserSession.expires_at < now))
    db.commit()
