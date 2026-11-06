"""Tables. No raw emails, codes or session tokens are stored: only keyed fingerprints and hashes.
Uploaded files are never stored; extraction results are stored encrypted (see web/crypto.py). A free scan's
PDF (a processed copy of the user's photos) is stored encrypted for a short time only (Batch.file_expires_at).

All times are naive UTC (same behaviour on SQLite and PostgreSQL).
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, false
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_fp: Mapped[str] = mapped_column(String(64), unique=True)  # HMAC of the normalized email
    lang: Mapped[str | None] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Page balance. Every change is also written to `ledger` (the auditable record); these are the running totals.
    trial_granted_at: Mapped[datetime | None] = mapped_column(DateTime)  # set once: one trial per user
    trial_pages_left: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    trial_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Pages added by the owner by hand (top-ups). They do not expire. See web/balance.py for the three buckets.
    paid_pages_left: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Human-friendly id like "SK-7F3K9Q" (web/accounts.py): the user quotes it to the owner, the admin and the
    # Telegram notice use it instead of any email. Nullable in the schema only because SQLite cannot add a NOT NULL
    # unique column without rebuilding `users` (which would cascade-delete child rows); always set in code.
    account_code: Mapped[str | None] = mapped_column(String(12), unique=True, index=True)
    # Business plan switched on by the owner (decision 46): key (start/biz/firm), monthly quota, and the plan's own
    # page bucket, which stops counting at plan_expires_at.
    plan: Mapped[str | None] = mapped_column(String(8))
    plan_quota: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    plan_pages_left: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime)  # set by the owner: no sign-in, no uploads
    # Fingerprint of the phone number confirmed in the Telegram bot (web/phone.py); never the number itself.
    # Unique (NULLs allowed on SQLite and PostgreSQL): one number belongs to one account.
    phone_fp: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)


class LoginCode(Base):
    __tablename__ = "login_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_fp: Mapped[str] = mapped_column(String(64), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))  # HMAC(code key, id | email_fp | code)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    ip_fp: Mapped[str | None] = mapped_column(String(64))


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of the cookie token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    ip_fp: Mapped[str | None] = mapped_column(String(64))
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))


class RateEvent(Base):
    """Counters for rate limits (per IP fingerprint). In the DB so they survive restarts and work across workers."""

    __tablename__ = "rate_events"
    __table_args__ = (Index("ix_rate_events_lookup", "bucket", "key_fp", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16))
    key_fp: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class Batch(Base):
    """One upload: counts and status in clear, extracted data only as an encrypted blob. No file names in clear.

    Who may see it (web/company.py): its author (`user_id`) while `org_id` is NULL (a personal batch), or every
    current member of the company `org_id`. `wallet_user_id` is whose page balance pays for it, fixed when the batch
    is accepted: the author outside a company, the company owner inside one.
    """

    __tablename__ = "batches"
    __table_args__ = (Index("ix_batches_user_created", "user_id", "created_at"),
                      Index("ix_batches_org_created", "org_id", "created_at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)  # random, used in URLs
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))  # the author
    # Company batch (shared history). NULL: personal. Set to NULL when the company is deleted (back to the author).
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="SET NULL"))
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))  # = user_id
    wallet_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    doctype: Mapped[str] = mapped_column(String(16))  # "" for the PDF formats (no document type there)
    # Format chosen at upload. Tables (xlsx, 1c, csv, json): every table format can be downloaded.
    # pdf_scan: free scanned PDF, no recognition. pdf_text: recognised text rebuilt as a PDF (charged).
    fmt: Mapped[str] = mapped_column(String(16))
    one_document: Mapped[bool] = mapped_column(Boolean, default=False)
    # Fixed at upload: the paying wallet had no plan and no top-up pages, only trial ones. Its reservation also
    # counts against the trial part of the daily cap (DailyUsage.trial_pages), and so does its correction.
    trial_funded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Recognised with the user's own provider key: nothing is charged, no page balance or site cap is used.
    byok: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    lang: Mapped[str] = mapped_column(String(2))  # interface language at upload: language of exported files
    status: Mapped[str] = mapped_column(String(10), index=True)  # queued, running, done, failed
    files_count: Mapped[int] = mapped_column(Integer, default=0)
    files_done: Mapped[int] = mapped_column(Integer, default=0)
    pages_estimated: Mapped[int] = mapped_column(Integer, default=0)
    pages_charged: Mapped[int] = mapped_column(Integer, default=0)
    work_units: Mapped[int] = mapped_column(Integer, default=0)  # what the batch cost us, in pages
    docs_found: Mapped[int] = mapped_column(Integer, default=0)
    needs_review: Mapped[int] = mapped_column(Integer, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, default=0)
    not_found: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(32))  # a safe label, never an exception message
    created_at: Mapped[datetime] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # results are purged after this
    results_enc: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)  # loaded only when read
    # pdf_scan only: the finished PDF, encrypted (web/crypto.py), kept a short time (file_expires_at), then
    # set to NULL; the history row stays. Tables and pdf_text keep their data in results_enc instead.
    file_enc: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    file_expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)


class LedgerEntry(Base):
    """Every change of a page balance: trial grants, batch charges, plan grants, top-ups, admin changes. Kept when a
    batch is deleted (`batch_ref` is the batch's public id, not a foreign key) and when the user deletes the account
    (`user_id` becomes NULL), so money questions can always be answered. The deltas are changes of the stored
    columns: their sums per user equal trial_pages_left, plan_pages_left and paid_pages_left."""

    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    batch_ref: Mapped[str | None] = mapped_column(String(32), index=True)
    # trial_grant, trial_bonus (after the phone was confirmed), batch, scan_free (a free scan: all zeros), plan_grant, plan_expire, topup, admin_adjust
    reason: Mapped[str] = mapped_column(String(16))
    trial_delta: Mapped[int] = mapped_column(Integer, default=0)
    paid_delta: Mapped[int] = mapped_column(Integer, default=0)
    plan_delta: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    note: Mapped[str | None] = mapped_column(String(200))  # the owner's note on manual changes
    charged: Mapped[int] = mapped_column(Integer, default=0)  # pages owed by the billing rules
    overrun: Mapped[int] = mapped_column(Integer, default=0)  # pages owed but not covered by the balance
    work_units: Mapped[int] = mapped_column(Integer, default=0)
    free_failures: Mapped[int] = mapped_column(Integer, default=0)  # failed jobs not charged (daily allowance)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class DailyUsage(Base):
    """Pages of work per UTC day across all website users (the owner's daily spending cap)."""

    __tablename__ = "daily_usage"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD
    pages: Mapped[int] = mapped_column(Integer, default=0)  # all charged batches (trial-funded included)
    # The part of `pages` taken by trial-funded batches (Batch.trial_funded); capped by WEB_TRIAL_DAILY_PAGES.
    trial_pages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class ScanUsage(Base):
    """Free scan pages per user per UTC day (the per-user allowance). Counters only, no document data."""

    __tablename__ = "scan_usage"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD
    pages: Mapped[int] = mapped_column(Integer, default=0)


class ScanDaily(Base):
    """Free scan pages per UTC day across all users: protects the server's CPU (not money)."""

    __tablename__ = "scan_daily"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    pages: Mapped[int] = mapped_column(Integer, default=0)


class PaymentRequest(Base):
    """"Хочу оплатить" (decision 46). Company, contact and comment only inside `data_enc` (web/crypto.py,
    key "requests", bound to the row id). Kept 90 days after it is marked done, 365 days if never handled."""

    __tablename__ = "payment_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(8), index=True)  # new, done
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)  # the Telegram notice was accepted
    data_enc: Mapped[bytes | None] = mapped_column(LargeBinary)


class AdminAudit(Base):
    """What the owner did in /admin. Never contact data; `detail` is a short fixed-format text."""

    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_fp: Mapped[str] = mapped_column(String(16))  # first 16 hex characters of the admin's email fingerprint
    action: Mapped[str] = mapped_column(String(24))
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    target_code: Mapped[str | None] = mapped_column(String(12))  # stays readable after the user is deleted
    detail: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class Tombstone(Base):
    """Left behind when an account is deleted: only the email fingerprint and whether the trial was used, so that
    deleting and registering again does not start a second trial."""

    __tablename__ = "tombstones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_fp: Mapped[str] = mapped_column(String(64), unique=True)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime)


class PhoneChallenge(Base):
    """The one-time Telegram link that confirms a phone (web/phone.py). One per user: a new link replaces the old.
    Only the hash of the token is stored; 15 minutes, one use. `outcome` is what the confirmation returned."""

    __tablename__ = "phone_challenges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    ip_fp: Mapped[str | None] = mapped_column(String(64))  # the browser's IP fingerprint: the per-IP trial cap
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    outcome: Mapped[str | None] = mapped_column(String(16))


class Organization(Base):
    """A company cabinet (decision 56). The page balance of the company is the owner's own balance (users row of
    owner_user_id). The name is the owner's choice (2-80 characters, no control characters, nothing like an email)."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)  # random
    name: Mapped[str] = mapped_column(String(80))
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Membership(Base):
    """A user in a company. user_id is unique: at most one company per user (v1). role: owner or member."""

    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Invitation(Base):
    """An invitation to a company. kind "link": a one-time link, only the token's hash is kept. kind "id": addressed
    to an account code; target_user_id is NULL when no such account exists or the person already has too many
    invitations (the owner sees the same either way). status: pending, accepted, declined, cancelled, expired.
    No emails, no phone numbers."""

    __tablename__ = "invitations"
    __table_args__ = (Index("ix_invitations_org_status", "org_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(4))
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    target_code: Mapped[str | None] = mapped_column(String(12))  # the code the owner typed, shown back to the owner
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class PhoneTrial(Base):
    """"This number already had a trial": only the fingerprint. Kept when the account is deleted (like the email
    tombstone); the owner can remove it in /admin when a number changed hands."""

    __tablename__ = "phone_trials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_fp: Mapped[str] = mapped_column(String(64), unique=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime)
    first_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
