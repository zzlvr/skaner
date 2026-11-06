"""One trial per phone number, confirmed through the Telegram bot (decision 43). Off unless WEB_PHONE_VERIFY=1.

When it is on:
- the first cabinet visit grants a small instant trial (WEB_TRIAL_INSTANT_PAGES, default 10) instead of the full one;
- the rest (trial_pages - instant) is a bonus added when the user confirms a phone number in the bot. The trial's
  end date does not move. A number that already gave a bonus (to this or any other account, also a deleted one)
  gives none again: the user keeps the instant pages;
- accounts that already had a trial before this feature keep it and are never asked for a number; the demo account
  (development only) counts as confirmed.

What is stored: never the number. users.phone_fp and phone_trials.phone_fp hold the fingerprint the bot computed
(HMAC with a key only the bot side derives from PHONE_VERIFY_SECRET). phone_trials survives account deletion: it
is the "this number already had a trial" record. Challenges store only the hash of the link token.

The confirmation arrives at POST /internal/phone-verified (web/phone_routes.py), signed by the bot with
PHONE_VERIFY_SECRET. That path must never be reachable from the internet: the reverse proxy blocks it
(nginx: `location /internal/ { deny all; }`) and the bot calls the app over localhost (WEB_INTERNAL_URL).
"""

import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bot import phone_verify as proto

from . import balance as bal
from . import security as sec
from .config import Settings
from .models import LedgerEntry, PhoneChallenge, PhoneTrial, RateEvent, User

CHALLENGE_TTL = timedelta(minutes=15)
CHALLENGES_PER_USER_DAY = 5
CHALLENGES_PER_IP_DAY = 20
CONFIRM_PER_MINUTE = 60  # the internal endpoint, all requests together
CONFIRM_PER_TOKEN_MINUTE = 10
MINUTE, DAY = timedelta(minutes=1), timedelta(days=1)

BUCKET_USER = "phone_user"
BUCKET_IP = "phone_ip"
BUCKET_ALL = "phone_all"
BUCKET_TOKEN = "phone_tok"
REASON_BONUS = "trial_bonus"  # ledger: the pages added after the phone was confirmed
NOTE_BONUS = "телефон подтверждён"


# ---------- who has to confirm ----------

def bonus_pages(settings: Settings) -> int:
    return max(0, settings.trial_pages - settings.trial_instant_pages)


def is_demo(user: User, demo_fp: str | None) -> bool:
    return demo_fp is not None and user.email_fp == demo_fp


def first_grant_pages(settings: Settings, user: User, demo_fp: str | None) -> int:
    """Pages of the automatic grant at the first cabinet visit (ensure_trial)."""
    if not settings.phone_verify or user.phone_fp is not None or is_demo(user, demo_fp):
        return settings.trial_pages
    return min(settings.trial_instant_pages, settings.trial_pages)


def bonus_due(db: Session, settings: Settings, user: User) -> bool:
    """The user got the instant trial (smaller than the full one), has no confirmed number and no bonus yet.
    Accounts with a full trial from before this feature, or a trial set by the owner, are not asked."""
    if user.trial_granted_at is None or user.phone_fp is not None or bonus_pages(settings) <= 0:
        return False
    grant = db.scalar(select(LedgerEntry.trial_delta).where(
        LedgerEntry.user_id == user.id, LedgerEntry.reason == "trial_grant").order_by(LedgerEntry.id).limit(1))
    if grant is None or grant >= settings.trial_pages:
        return False
    return not db.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.user_id == user.id, LedgerEntry.reason == REASON_BONUS))


def need_phone(db: Session, settings: Settings, user: User, now: datetime, demo_fp: str | None) -> bool:
    """Show the "confirm your phone" step: the feature is on and confirming would still give pages."""
    if not settings.phone_verify or user.phone_fp is not None or is_demo(user, demo_fp):
        return False
    if user.trial_granted_at is None:
        return True  # the instant trial was not granted yet (per-IP cap): confirming gives the full trial later
    return bal.trial_active(user, now) and bonus_due(db, settings, user)


# ---------- challenges (the Telegram link) ----------

def _count(db: Session, bucket: str, key: str, since: datetime) -> int:
    return db.scalar(select(func.count()).select_from(RateEvent).where(
        RateEvent.bucket == bucket, RateEvent.key_fp == key, RateEvent.created_at > since)) or 0


def create_challenge(db: Session, user: User, ip_fp: str, now: datetime) -> str | None:
    """A new one-time link token for this user (it replaces the old one). Returns the token, or None when a
    limit was reached. Only the token's hash is stored; the token is shown once, in the page that made it."""
    if _count(db, BUCKET_USER, bal.user_key(user.id), now - DAY) >= CHALLENGES_PER_USER_DAY or \
            _count(db, BUCKET_IP, ip_fp, now - DAY) >= CHALLENGES_PER_IP_DAY:
        return None
    token = secrets.token_urlsafe(24)  # 32 characters: fits Telegram's 64-character start parameter
    db.execute(delete(PhoneChallenge).where(PhoneChallenge.user_id == user.id))
    db.add(PhoneChallenge(user_id=user.id, token_hash=sec.token_hash(token), ip_fp=ip_fp, created_at=now,
                          expires_at=now + CHALLENGE_TTL))
    db.add(RateEvent(bucket=BUCKET_USER, key_fp=bal.user_key(user.id), created_at=now))
    db.add(RateEvent(bucket=BUCKET_IP, key_fp=ip_fp, created_at=now))
    try:
        db.commit()
    except IntegrityError:  # a parallel request made one at the same moment
        db.rollback()
        return None
    return token


def current_challenge(db: Session, user_id: int) -> PhoneChallenge | None:
    return db.scalar(select(PhoneChallenge).where(PhoneChallenge.user_id == user_id))


def challenge_state(ch: PhoneChallenge | None, now: datetime) -> str:
    """none, pending, expired, or the stored outcome of a used link (ok, phone_used, other_phone, invalid)."""
    if ch is None:
        return "none"
    if ch.used_at is not None:
        return ch.outcome or proto.INVALID
    return "pending" if ch.expires_at > now else "expired"


# ---------- the confirmation (internal endpoint) ----------

def limited_all(db: Session, now: datetime) -> bool:
    """All requests to the internal endpoint together (counted before anything else is checked)."""
    if _count(db, BUCKET_ALL, "all", now - MINUTE) >= CONFIRM_PER_MINUTE:
        return True
    db.add(RateEvent(bucket=BUCKET_ALL, key_fp="all", created_at=now))
    db.commit()
    return False


def limited_token(db: Session, token: str, now: datetime) -> bool:
    """Signed requests per token. The key is a hash with its own prefix, never the token or its stored hash."""
    key = sec.token_hash("rate|" + token)
    if _count(db, BUCKET_TOKEN, key, now - MINUTE) >= CONFIRM_PER_TOKEN_MINUTE:
        return True
    db.add(RateEvent(bucket=BUCKET_TOKEN, key_fp=key, created_at=now))
    db.commit()
    return False


def confirm(db: Session, settings: Settings, token: str, phone_fp: str, now: datetime) -> str:
    """Use the link token once and apply the confirmed number. Returns one of the proto statuses.

    1. The token is consumed with a conditional UPDATE (unused, unexpired): of parallel requests only one wins.
       Unknown, expired and used tokens all give INVALID.
    2. The number: a user keeps one number (the same number again is a no-op). A number found in phone_trials
       (any account, also deleted ones) gives no bonus. Otherwise, in one transaction: phone_trials row, users.phone_fp,
       the bonus pages with a ledger row. The unique indexes make a parallel second use of the number fail safely.
    """
    h = sec.token_hash(token)
    used = db.execute(update(PhoneChallenge)
                      .where(PhoneChallenge.token_hash == h, PhoneChallenge.used_at.is_(None),
                             PhoneChallenge.expires_at > now)
                      .values(used_at=now, outcome=proto.INVALID)
                      .execution_options(synchronize_session=False))
    if used.rowcount != 1:
        db.rollback()
        return proto.INVALID
    ch = db.scalar(select(PhoneChallenge).where(PhoneChallenge.token_hash == h))
    db.commit()  # the token is spent whatever happens next
    user = db.get(User, ch.user_id) if ch is not None else None
    if user is None or user.blocked_at is not None:
        return proto.INVALID
    result = _apply(db, settings, user, phone_fp, ch.ip_fp or "", now)
    db.execute(update(PhoneChallenge).where(PhoneChallenge.id == ch.id).values(outcome=result)
               .execution_options(synchronize_session=False))
    db.commit()
    return result


def _apply(db: Session, settings: Settings, user: User, phone_fp: str, ip_fp: str, now: datetime) -> str:
    if user.phone_fp is not None:
        return proto.OK if sec_equal(user.phone_fp, phone_fp) else proto.OTHER_PHONE
    if db.scalar(select(PhoneTrial.id).where(PhoneTrial.phone_fp == phone_fp)) is not None:
        return proto.PHONE_USED
    give_bonus = bonus_due(db, settings, user)
    db.add(PhoneTrial(phone_fp=phone_fp, granted_at=now, first_user_id=user.id))
    try:
        db.flush()
        bound = db.execute(update(User).where(User.id == user.id, User.phone_fp.is_(None))
                           .values(phone_fp=phone_fp).execution_options(synchronize_session=False))
        if bound.rowcount != 1:  # a parallel confirmation bound another number first
            db.rollback()
            return proto.OTHER_PHONE
        if give_bonus:
            pages = bonus_pages(settings)
            db.execute(update(User).where(User.id == user.id)
                       .values(trial_pages_left=User.trial_pages_left + pages)
                       .execution_options(synchronize_session=False))
            db.add(LedgerEntry(user_id=user.id, reason=REASON_BONUS, trial_delta=pages, paid_delta=0, charged=0,
                               overrun=0, work_units=0, free_failures=0, note=NOTE_BONUS, created_at=now))
        db.commit()
    except IntegrityError:  # the same number confirmed in parallel for another account
        db.rollback()
        return proto.PHONE_USED
    db.refresh(user)
    if user.trial_granted_at is None:  # the instant trial was held back by the per-IP cap: the full trial now
        bal.ensure_trial(db, settings, user, ip_fp, now, pages=settings.trial_pages, note=NOTE_BONUS)
    return proto.OK


def sec_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("ascii", "replace"), b.encode("ascii", "replace"))


# ---------- owner: remove the binding ----------

def has_binding(db: Session, user: User) -> bool:
    return user.phone_fp is not None or db.scalar(
        select(func.count()).select_from(PhoneTrial).where(PhoneTrial.first_user_id == user.id)) > 0


def reset(db: Session, user: User) -> bool:
    """Remove users.phone_fp and the number's phone_trials record (the person changed their number, or a number
    moved to someone else), plus a pending link. Pages already given stay. Returns False when there was nothing."""
    if not has_binding(db, user):
        return False
    conds = [PhoneTrial.first_user_id == user.id]
    if user.phone_fp is not None:
        conds.append(PhoneTrial.phone_fp == user.phone_fp)
    db.execute(delete(PhoneTrial).where(or_(*conds)))
    db.execute(delete(PhoneChallenge).where(PhoneChallenge.user_id == user.id))
    db.execute(update(User).where(User.id == user.id).values(phone_fp=None).execution_options(synchronize_session=False))
    db.commit()
    db.refresh(user)
    return True
