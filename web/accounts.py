"""Accounts: the plan catalogue (decision 40), human-friendly account codes, and full account deletion.

Deletion (by the user or by the owner) removes the user, sessions, batches with their encrypted results, scan
counters and payment requests. Two things stay on purpose:
- ledger rows, with user_id set to NULL: the money record for disputes (who was charged what and when);
- a tombstone with the email fingerprint and trial_used=True (only when the trial was granted): registering again
  with the same address does not start a second trial (web/auth.get_or_create_user reads it);
- phone_trials rows (web/phone.py), with first_user_id set to NULL: the number's trial stays used.
Companies (web/company.py): the user's membership and the invitations addressed to them are deleted. An owner can
delete the account only while nobody else is in the company (callers check web/company.account_delete_refusal);
the company is then deleted with the account.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    AdminAudit, Batch, Invitation, LedgerEntry, Membership, Organization, PaymentRequest, PhoneChallenge, PhoneTrial,
    ScanUsage, Tombstone, User, UserSession,
)

# No 0/O, 1/I/L, so the code can be read out over the phone. 31^6 ~ 887 million codes.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 6
CODE_RE = re.compile(r"SK-[" + CODE_ALPHABET + r"]{" + str(CODE_LEN) + r"}")
PLAN_DAYS = 30


@dataclass(frozen=True)
class Plan:
    key: str  # also the suffix of the locale keys plan_*, price_*, pages_*, over_*
    quota: int  # pages per 30 days
    price: int  # tenge per month
    over: int  # tenge per page beyond the quota (agreed with the owner, not billed automatically yet)


PLANS = {
    "start": Plan("start", 300, 12900, 45),
    "biz": Plan("biz", 1000, 34900, 40),
    "firm": Plan("firm", 3000, 99900, 38),
}
PLAN_NAMES = {"start": "Start", "biz": "Business", "firm": "Accounting firm"}  # admin and Telegram


def new_account_code() -> str:
    return "SK-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def clean_account_code(raw: str | None) -> str | None:
    """Accepts ' sk-7f3k9q ' (case, spaces); returns the canonical code or None."""
    if not isinstance(raw, str) or len(raw) > 32:
        return None
    s = raw.strip().upper()
    if not s.startswith("SK-"):
        s = "SK-" + s
    return s if CODE_RE.fullmatch(s) else None


def assign_code(db: Session, user: User) -> None:
    """Give a user without a code a fresh unique one (retries on the rare collision). Commits."""
    for _ in range(10):
        if user.account_code:
            return
        done = db.execute(update(User).where(User.id == user.id, User.account_code.is_(None))
                          .values(account_code=new_account_code()))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        db.refresh(user)
        if done.rowcount == 1 or user.account_code:
            return
    raise RuntimeError("could not assign an account code")


def by_code(db: Session, code: str | None) -> User | None:
    code = clean_account_code(code)
    return db.scalar(select(User).where(User.account_code == code)) if code else None


def delete_user(db: Session, user_id: int, now: datetime) -> bool:
    """Delete the account and its data (see the module docstring). Explicit deletes, not only the database's
    ON DELETE rules, so the result is the same on every backend. Returns False when the user was already gone."""
    user = db.get(User, user_id)
    if user is None:
        return False
    from .company import _drop_org  # here: web/company imports this module

    owned = db.scalar(select(Organization).where(Organization.owner_user_id == user_id))
    if owned is not None:
        others = db.scalar(select(Membership.id).where(Membership.org_id == owned.id, Membership.user_id != user_id)
                           .limit(1))
        if others is not None:
            raise RuntimeError("the owner of a company with members cannot be deleted")
        _drop_org(db, owned.id)
    db.execute(delete(Membership).where(Membership.user_id == user_id))
    db.execute(delete(Invitation).where(Invitation.target_user_id == user_id))
    db.execute(update(Invitation).where(Invitation.created_by == user_id).values(created_by=None))
    db.execute(update(Batch).where(Batch.wallet_user_id == user_id, Batch.user_id != user_id)
               .values(wallet_user_id=None))
    db.execute(update(Batch).where(Batch.created_by_user_id == user_id, Batch.user_id != user_id)
               .values(created_by_user_id=None))
    if user.trial_granted_at is not None:
        stone = db.scalar(select(Tombstone).where(Tombstone.email_fp == user.email_fp))
        if stone is None:
            db.add(Tombstone(email_fp=user.email_fp, trial_used=True, deleted_at=now))
        else:
            stone.trial_used, stone.deleted_at = True, now
    db.execute(update(LedgerEntry).where(LedgerEntry.user_id == user_id).values(user_id=None))
    db.execute(update(AdminAudit).where(AdminAudit.target_user_id == user_id).values(target_user_id=None))
    db.execute(update(PhoneTrial).where(PhoneTrial.first_user_id == user_id).values(first_user_id=None))
    db.execute(delete(PhoneChallenge).where(PhoneChallenge.user_id == user_id))
    db.execute(delete(PaymentRequest).where(PaymentRequest.user_id == user_id))
    db.execute(delete(Batch).where(Batch.user_id == user_id))
    db.execute(delete(ScanUsage).where(ScanUsage.user_id == user_id))
    db.execute(delete(UserSession).where(UserSession.user_id == user_id))
    db.execute(delete(User).where(User.id == user_id))
    db.commit()
    return True


def trial_used_before(db: Session, email_fp: str) -> bool:
    return bool(db.scalar(select(Tombstone.trial_used).where(Tombstone.email_fp == email_fp)))
