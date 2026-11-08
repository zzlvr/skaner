"""Page balance on the website: trial grant, plans, top-ups, limits, charging. The charging rules themselves are
bot/billing.py.

Three page buckets on the user row, spent in this order (the one that expires first goes first):
1. trial_pages_left: granted once per user (50 pages, 14 days; whichever ends first), at most N new trials per IP
   a day. 2. plan_pages_left: a business plan switched on by the owner in /admin (payment is manual). Activation adds the plan's quota and sets plan_expires_at = now + 30 days. From
   that moment on the bucket stops counting (balance() shows 0); the stored number is zeroed with a "plan_expire"
   ledger row the next time the account is touched (expire_plan). Unused plan pages do not survive the expiry.
3. paid_pages_left: pages the owner adds by hand (top-ups). They never expire.
Rules that hold for every change:
- The balance never goes below zero: what a batch owes beyond the buckets is recorded as `overrun` in the ledger.
- No automatic billing beyond a plan for now: extra pages are agreed with the owner, who tops them up by hand.
- Every balance change writes a ledger row (reason, the column deltas, the owner's note for manual changes); a
  batch writes one row even when nothing was charged (a free scan writes a row with reason "scan_free" and zeros).
- Charged work shares a site-wide daily cap (WEB_DAILY_PAGES). Batches paid only by trial pages (trial_funded)
  may use at most WEB_TRIAL_DAILY_PAGES of it, so trial farming cannot starve paying customers.
- Free scans do not touch the page balance: they have their own allowance per user per UTC day and a separate
  site-wide daily cap that protects the CPU (scans run locally, they cost no money).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .billing_rules import FREE_FAILURES_PER_DAY, Job, settle

from .accounts import PLAN_DAYS, PLANS
from .config import Settings
from .models import DailyUsage, LedgerEntry, RateEvent, ScanDaily, ScanUsage, User

BUCKET_TRIAL = "trial_ip"
BUCKET_BATCH = "batch_user"
HOUR, DAY = timedelta(hours=1), timedelta(days=1)


@dataclass
class Balance:
    trial_left: int  # 0 when the trial has expired
    paid_left: int  # manual top-ups, no expiry
    trial_expires_at: datetime | None
    trial_state: str  # active, expired, used, none
    plan_left: int = 0  # 0 when the plan has expired
    plan: str | None = None
    plan_quota: int = 0
    plan_expires_at: datetime | None = None
    plan_state: str = "none"  # active, expired, used (pages gone, period not over), none

    @property
    def total(self) -> int:
        return self.trial_left + self.plan_left + self.paid_left

    @property
    def show_pay(self) -> bool:
        """Show "Хочу оплатить": no pages left, or neither a live trial nor a live plan."""
        return self.total <= 0 or (self.plan_state != "active" and self.trial_state != "active")

    def days_left(self, now: datetime) -> int:
        if self.trial_state != "active" or self.trial_expires_at is None:
            return 0
        return max(0, -(-int((self.trial_expires_at - now).total_seconds()) // 86400))


def trial_active(user: User, at: datetime) -> bool:
    return user.trial_expires_at is not None and at < user.trial_expires_at


def plan_active(user: User, at: datetime) -> bool:
    return user.plan is not None and user.plan_expires_at is not None and at < user.plan_expires_at


def balance(user: User, now: datetime) -> Balance:
    if user.trial_granted_at is None:
        state, trial = "none", 0
    elif not trial_active(user, now):
        state, trial = "expired", 0
    elif user.trial_pages_left <= 0:
        state, trial = "used", 0
    else:
        state, trial = "active", user.trial_pages_left
    if user.plan is None:
        pstate, plan = "none", 0
    elif not plan_active(user, now):
        pstate, plan = "expired", 0
    elif user.plan_pages_left <= 0:
        pstate, plan = "used", 0
    else:
        pstate, plan = "active", user.plan_pages_left
    return Balance(trial, max(0, user.paid_pages_left), user.trial_expires_at, state, plan, user.plan,
                   user.plan_quota or 0, user.plan_expires_at, pstate)


def user_key(user_id: int) -> str:
    return f"user:{user_id}"


def _count_events(db: Session, bucket: str, key: str, since: datetime) -> int:
    return db.scalar(select(func.count()).select_from(RateEvent).where(
        RateEvent.bucket == bucket, RateEvent.key_fp == key, RateEvent.created_at > since)) or 0


def ensure_trial(db: Session, settings: Settings, user: User, ip_fp: str, now: datetime, pages: int | None = None,
                 note: str | None = None) -> bool:
    """Grant the trial on the first visit of the cabinet. Returns True when it was granted now.
    `pages` defaults to the full trial (web/phone.py passes the instant part when phone confirmation is on).

    Atomic (conditional UPDATE on trial_granted_at IS NULL): a user can never get two trials.
    When the IP already made too many trials today, nothing is granted; the next visit tries again.
    """
    if user.trial_granted_at is not None:
        return False
    if (settings.trial_pages if pages is None else pages) <= 0:  # no trial offered: nothing to grant or record
        return False
    if _count_events(db, BUCKET_TRIAL, ip_fp, now - DAY) >= settings.trials_per_ip_day:
        return False
    pages = settings.trial_pages if pages is None else pages
    done = db.execute(
        update(User)
        .where(User.id == user.id, User.trial_granted_at.is_(None))
        .values(trial_granted_at=now, trial_pages_left=pages,
                trial_expires_at=now + timedelta(days=settings.trial_days))
    )
    if done.rowcount != 1:
        db.rollback()
        db.refresh(user)
        return False
    db.add(LedgerEntry(user_id=user.id, reason="trial_grant", trial_delta=pages, paid_delta=0,
                       charged=0, overrun=0, work_units=0, free_failures=0, note=note, created_at=now))
    db.add(RateEvent(bucket=BUCKET_TRIAL, key_fp=ip_fp, created_at=now))
    db.commit()
    db.refresh(user)
    return True


def trial_blocked_by_ip(db: Session, settings: Settings, user: User, ip_fp: str, now: datetime) -> bool:
    return user.trial_granted_at is None and \
        _count_events(db, BUCKET_TRIAL, ip_fp, now - DAY) >= settings.trials_per_ip_day


# ---------- per-user batch rate ----------

def batches_last_hour(db: Session, user_id: int, now: datetime) -> int:
    """Counted from rate events, not from batches: deleting a batch must not free a slot."""
    return _count_events(db, BUCKET_BATCH, user_key(user_id), now - HOUR)


def record_batch(db: Session, user_id: int, now: datetime) -> None:
    db.add(RateEvent(bucket=BUCKET_BATCH, key_fp=user_key(user_id), created_at=now))


# ---------- global daily cap ----------

def day_key(at: datetime) -> str:
    return at.date().isoformat()


def daily_used(db: Session, at: datetime) -> int:
    return db.scalar(select(DailyUsage.pages).where(DailyUsage.day == day_key(at))) or 0


def daily_trial_used(db: Session, at: datetime) -> int:
    return db.scalar(select(DailyUsage.trial_pages).where(DailyUsage.day == day_key(at))) or 0


def trial_funded(b: Balance) -> bool:
    """A batch is trial-funded when the paying wallet has no usable plan pages and no top-up pages: only trial
    pages could cover it. Any paid page at all makes it paid-funded (a paying customer is never limited by the
    trial part of the daily cap, even when trial pages are spent first)."""
    return b.plan_left <= 0 and b.paid_left <= 0


def reserve_daily(db: Session, settings: Settings, pages: int, now: datetime, trial: bool = False) -> str | None:
    """Reserve pages of today's global cap and, for a trial-funded batch, of its trial part too. One conditional
    UPDATE checks both, so parallel uploads cannot overshoot either. Returns None when reserved, else the refusal
    code: "daily" (the whole cap is full) or "trial_daily" (only the trial part is)."""
    day = day_key(now)
    if db.get(DailyUsage, day) is None:
        db.add(DailyUsage(day=day, pages=0, trial_pages=0))
        try:
            db.commit()
        except IntegrityError:  # created by a parallel request
            db.rollback()
    cond = [DailyUsage.day == day, DailyUsage.pages + pages <= settings.daily_pages]
    values = {"pages": DailyUsage.pages + pages}
    if trial:
        cond.append(DailyUsage.trial_pages + pages <= settings.trial_daily_pages)
        values["trial_pages"] = DailyUsage.trial_pages + pages
    done = db.execute(update(DailyUsage).where(*cond).values(**values))
    db.commit()
    if done.rowcount == 1:
        return None
    return "daily" if daily_used(db, now) + pages > settings.daily_pages else "trial_daily"


def adjust_daily(db: Session, day: str, delta: int, trial: bool = False) -> None:
    """Replace a reservation by what the batch really cost (may go above the cap: that is only accounting).
    A trial-funded batch corrects the trial part by the same delta."""
    if delta:
        values = {"pages": DailyUsage.pages + delta}
        if trial:
            values["trial_pages"] = DailyUsage.trial_pages + delta
        db.execute(update(DailyUsage).where(DailyUsage.day == day).values(**values))


# ---------- free scans ----------

def scan_used(db: Session, user_id: int, at: datetime) -> int:
    return db.scalar(select(ScanUsage.pages).where(ScanUsage.user_id == user_id, ScanUsage.day == day_key(at))) or 0


def scan_left(db: Session, settings: Settings, user_id: int, at: datetime) -> int:
    return max(0, settings.free_scan_pages_day - scan_used(db, user_id, at))


def scan_site_used(db: Session, at: datetime) -> int:
    return db.scalar(select(ScanDaily.pages).where(ScanDaily.day == day_key(at))) or 0


def _ensure_row(db: Session, model, **key) -> None:
    if db.get(model, tuple(key.values()) if len(key) > 1 else next(iter(key.values()))) is None:
        db.add(model(pages=0, **key))
        try:
            db.commit()
        except IntegrityError:  # created by a parallel request
            db.rollback()


def reserve_scan(db: Session, settings: Settings, user_id: int, pages: int, now: datetime) -> str | None:
    """Take `pages` from the user's free scan allowance and from the site's daily scan cap (conditional UPDATEs:
    parallel uploads cannot overshoot). Returns None when both were taken, else the refusal code."""
    day = day_key(now)
    _ensure_row(db, ScanUsage, user_id=user_id, day=day)
    mine = db.execute(
        update(ScanUsage)
        .where(ScanUsage.user_id == user_id, ScanUsage.day == day,
               ScanUsage.pages + pages <= settings.free_scan_pages_day)
        .values(pages=ScanUsage.pages + pages)
    )
    db.commit()
    if mine.rowcount != 1:
        return "scan_daily"
    _ensure_row(db, ScanDaily, day=day)
    site = db.execute(
        update(ScanDaily)
        .where(ScanDaily.day == day, ScanDaily.pages + pages <= settings.scan_pages_day)
        .values(pages=ScanDaily.pages + pages)
    )
    if site.rowcount != 1:
        adjust_scan(db, user_id, day, -pages, site_too=False)
        db.commit()
        return "daily"
    db.commit()
    return None


def adjust_scan(db: Session, user_id: int, day: str, delta: int, site_too: bool = True) -> None:
    """Give back reserved scan pages (a scan that was refused or failed). Does not commit."""
    if not delta:
        return
    db.execute(update(ScanUsage).where(ScanUsage.user_id == user_id, ScanUsage.day == day)
               .values(pages=ScanUsage.pages + delta))
    if site_too:
        db.execute(update(ScanDaily).where(ScanDaily.day == day).values(pages=ScanDaily.pages + delta))


# ---------- charging ----------

def free_failures_used(db: Session, user_id: int, now: datetime) -> int:
    start = datetime(now.year, now.month, now.day)
    return db.scalar(select(func.coalesce(func.sum(LedgerEntry.free_failures), 0)).where(
        LedgerEntry.user_id == user_id, LedgerEntry.created_at >= start)) or 0


@dataclass
class Charge:
    charged: int  # pages owed by the rules
    taken_trial: int
    taken_paid: int
    overrun: int  # owed but not covered (balance never goes below zero)
    work_units: int
    free_failures: int
    taken_plan: int = 0


def charge(db: Session, user_id: int, batch_ref: str, jobs: list[Job], started: datetime, now: datetime) -> Charge:
    """Settle a finished batch with bot.billing.settle and take the pages: trial, then plan, then top-ups.

    The trial and the plan count as valid if they were valid when the batch was accepted (`started`).
    Does not commit: the caller commits the charge together with the batch's final state (one transaction).
    """
    free_left = max(0, FREE_FAILURES_PER_DAY - free_failures_used(db, user_id, now))
    bill = settle(jobs, free_left)
    take_trial = take_plan = take_paid = 0
    gone = False
    for _ in range(3):  # conditional UPDATE; re-read if the balance changed in between
        user = db.get(User, user_id, populate_existing=True)
        if user is None:  # account deleted while processing: the ledger row is kept without a user
            gone = True
            break
        trial = max(0, user.trial_pages_left) if trial_active(user, started) else 0
        plan = max(0, user.plan_pages_left) if plan_active(user, started) else 0
        want_trial = min(bill.charged, trial)
        want_plan = min(bill.charged - want_trial, plan)
        want_paid = min(bill.charged - want_trial - want_plan, max(0, user.paid_pages_left))
        done = db.execute(
            update(User)
            .where(User.id == user_id, User.trial_pages_left == user.trial_pages_left,
                   User.plan_pages_left == user.plan_pages_left, User.paid_pages_left == user.paid_pages_left)
            .values(trial_pages_left=User.trial_pages_left - want_trial,
                    plan_pages_left=User.plan_pages_left - want_plan,
                    paid_pages_left=User.paid_pages_left - want_paid)
            .execution_options(synchronize_session=False)
        )
        if done.rowcount == 1:
            take_trial, take_plan, take_paid = want_trial, want_plan, want_paid
            break
    taken = take_trial + take_plan + take_paid
    result = Charge(bill.charged, take_trial, take_paid, bill.charged - taken, bill.work_units,
                    bill.free_failures_used, take_plan)
    db.add(LedgerEntry(
        user_id=None if gone else user_id, batch_ref=batch_ref, reason="batch", trial_delta=-take_trial,
        plan_delta=-take_plan, paid_delta=-take_paid, charged=result.charged, overrun=result.overrun,
        work_units=result.work_units, free_failures=result.free_failures, created_at=now,
    ))
    return result


# ---------- manual changes by the owner (/admin) ----------
# Each one is one transaction: the user row change and its ledger row are committed together. Page columns change
# by SQL expressions (x = x + n), so a batch charged at the same moment cannot lose an update: charge() above
# compares the old values and retries when the row changed under it.

MAX_TOPUP = 100_000
MAX_TRIAL_PAGES = 1000
MAX_TRIAL_DAYS = 60
MAX_EXTEND_DAYS = 366


def _ledger(db: Session, user_id: int, reason: str, note: str | None, now: datetime, trial: int = 0, plan: int = 0,
            paid: int = 0) -> None:
    db.add(LedgerEntry(user_id=user_id, reason=reason, trial_delta=trial, plan_delta=plan, paid_delta=paid,
                       charged=0, overrun=0, work_units=0, free_failures=0, note=note or None, created_at=now))


def expire_plan(db: Session, user: User, now: datetime, commit: bool = True) -> int:
    """Zero the pages of a plan whose period is over and record it ("plan_expire"). Returns the pages removed."""
    if user.plan is None or plan_active(user, now) or user.plan_pages_left == 0:
        return 0
    left = user.plan_pages_left
    done = db.execute(update(User).where(User.id == user.id, User.plan_pages_left == left,
                                         User.plan_expires_at <= now)
                      .values(plan_pages_left=0).execution_options(synchronize_session=False))
    if done.rowcount == 1:
        _ledger(db, user.id, "plan_expire", None, now, plan=-left)
    if commit:
        db.commit()
        db.refresh(user)
    return left if done.rowcount == 1 else 0


def grant_plan(db: Session, user: User, plan_key: str, note: str | None, now: datetime) -> None:
    """Switch a plan on for 30 days from now and add its quota. Pages of a plan that is still running are kept
    (the quota is added to them); pages of an expired plan are removed first."""
    plan = PLANS[plan_key]
    expire_plan(db, user, now, commit=False)
    db.execute(update(User).where(User.id == user.id)
               .values(plan=plan.key, plan_quota=plan.quota, plan_expires_at=now + timedelta(days=PLAN_DAYS),
                       plan_pages_left=User.plan_pages_left + plan.quota)
               .execution_options(synchronize_session=False))
    _ledger(db, user.id, "plan_grant", note, now, plan=plan.quota)
    db.commit()
    db.refresh(user)


def extend_plan(db: Session, user: User, days: int, note: str | None, now: datetime) -> bool:
    """Move the end of a running plan by `days`. An expired plan is not revived (its pages are gone): activate a
    plan again instead. Returns False when there is no running plan."""
    if not 1 <= days <= MAX_EXTEND_DAYS:
        raise ValueError("days out of range")
    if not plan_active(user, now):
        return False
    db.execute(update(User).where(User.id == user.id)
               .values(plan_expires_at=user.plan_expires_at + timedelta(days=days))
               .execution_options(synchronize_session=False))
    _ledger(db, user.id, "admin_adjust", note, now)  # the period changed, the pages did not
    db.commit()
    db.refresh(user)
    return True


def end_plan(db: Session, user: User, note: str | None, now: datetime) -> bool:
    """End the plan now; its remaining pages are removed. Returns False when there is no plan."""
    if user.plan is None:
        return False
    for _ in range(5):  # conditional UPDATE: a batch charged in between changes the number; read it again
        db.refresh(user)
        left = user.plan_pages_left
        done = db.execute(update(User).where(User.id == user.id, User.plan_pages_left == left)
                          .values(plan_expires_at=min(now, user.plan_expires_at or now), plan_pages_left=0)
                          .execution_options(synchronize_session=False))
        if done.rowcount == 1:
            _ledger(db, user.id, "admin_adjust", note, now, plan=-left)
            db.commit()
            db.refresh(user)
            return True
        db.rollback()
    raise RuntimeError("balance kept changing")


def topup(db: Session, user: User, pages: int, note: str | None, now: datetime) -> None:
    """Add pages that never expire (1..MAX_TOPUP)."""
    if not 1 <= pages <= MAX_TOPUP:
        raise ValueError("top-up out of range")
    db.execute(update(User).where(User.id == user.id).values(paid_pages_left=User.paid_pages_left + pages)
               .execution_options(synchronize_session=False))
    _ledger(db, user.id, "topup", note, now, paid=pages)
    db.commit()
    db.refresh(user)


def set_trial(db: Session, user: User, pages: int, days: int, note: str | None, now: datetime) -> None:
    """Set the trial to `pages` pages for `days` days from now (0 days ends it). Marks the trial as granted, so the
    automatic grant (ensure_trial) never adds a second one."""
    if not (0 <= pages <= MAX_TRIAL_PAGES and 0 <= days <= MAX_TRIAL_DAYS):
        raise ValueError("trial out of range")
    for _ in range(5):  # conditional UPDATE, as in end_plan
        db.refresh(user)
        old = user.trial_pages_left
        done = db.execute(update(User).where(User.id == user.id, User.trial_pages_left == old)
                          .values(trial_granted_at=user.trial_granted_at or now, trial_pages_left=pages,
                                  trial_expires_at=now + timedelta(days=days))
                          .execution_options(synchronize_session=False))
        if done.rowcount == 1:
            _ledger(db, user.id, "admin_adjust", note, now, trial=pages - old)
            db.commit()
            db.refresh(user)
            return
        db.rollback()
    raise RuntimeError("balance kept changing")
