"""Company cabinets: one owner, members, invitations, the shared page balance, who sees which batch.

Rules:
- A user is in at most one company (memberships.user_id is unique). A user in no company works exactly as before.
- Any signed-in user can create a company, also during the trial; the creator is its owner.
- Money: the company's page balance IS the owner's balance (trial, plan and top-up buckets on the owner's users row).
  A batch of any member is paid from the owner's balance (Batch.wallet_user_id = owner, fixed at upload); the
  member's own balance is not used while they are in the company. Two members uploading at the same moment cannot
  together reserve more pages than the owner has (wallet_reserved below, checked after the batch row is written).
- People per company, the owner included: settings.org_max_trial without an active plan, settings.org_max_paid
  with one. When a plan ends nobody is removed, but nobody new can join above the lower limit.
- Who sees a batch (owner's decision, 2026-10-21):
  * no company: own personal batches (org_id NULL);
  * a member: only the company batches they made themselves (not colleagues', and not their personal batches from
    before joining while they are in the company);
  * the owner: every batch of the company plus their own personal batches.
  Whoever sees a batch may also delete it (a member: own batches; the owner: any company batch). Anything else is a
  404, the same as an unknown id. After leaving or being removed, a member keeps no access to the company batches
  they made (the owner still has them); their personal batches from before joining come back.
- Deleting a company: refused while a company batch is running. Members are removed, invitations deleted, and each
  company batch goes back to its author as a personal batch (org_id NULL): nothing is destroyed.
- Invitations never collect emails. A one-time link (/join/<token>, only the token's hash is stored, 7 days, single
  use) or an account code SK-XXXXXX: the person sees the invitation in the cabinet and access opens only after they
  press Accept. A typed code gets the same reply and the same row in the owner's list whether or not the account
  exists (target_user_id NULL then), so codes cannot be probed. Limits: 20 live invitations per company, 20 new
  per owner per day, 5 live invitations per invited person (extra ones are silently not delivered).
- Logs: counts and outcomes only.
"""

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import accounts
from . import balance as bal
from . import payments
from . import security as sec
from .config import Settings
from .jobs import ACTIVE
from .models import Batch, Invitation, Membership, Organization, RateEvent, User
from .pdf_formats import SCAN

log = logging.getLogger("skaner.web.company")

OWNER, MEMBER = "owner", "member"
NAME_MIN, NAME_MAX = 2, 80
INVITE_TTL = timedelta(days=7)
KEEP_INVITES = timedelta(days=30)  # finished invitations are deleted this long after their end
LIVE_PER_ORG = 20
NEW_PER_OWNER_DAY = 20
LIVE_PER_USER = 5
BUCKET_INVITE = "org_invite"
DAY = timedelta(days=1)


class NameRefused(Exception):
    pass


@dataclass
class Ctx:
    """The signed-in user's company situation, read from the database on every request."""

    user: User
    org: Organization | None = None
    membership: Membership | None = None
    wallet: User | None = None  # whose pages are used: the owner inside a company, the user outside

    @property
    def in_org(self) -> bool:
        return self.org is not None

    @property
    def is_owner(self) -> bool:
        return self.membership is not None and self.membership.role == OWNER

    @property
    def is_member(self) -> bool:
        return self.membership is not None and self.membership.role == MEMBER


def membership_of(db: Session, user_id: int) -> Membership | None:
    return db.scalar(select(Membership).where(Membership.user_id == user_id))


def context(db: Session, user: User) -> Ctx:
    m = membership_of(db, user.id)
    if m is None:
        return Ctx(user, wallet=user)
    org = db.get(Organization, m.org_id)
    if org is None:  # cannot happen (foreign key), but never fall back to someone else's wallet
        return Ctx(user, wallet=user)
    wallet = user if org.owner_user_id == user.id else db.get(User, org.owner_user_id)
    return Ctx(user, org, m, wallet)


def clean_name(raw: str) -> str:
    try:
        name = payments._clean(raw or "", NAME_MAX)
    except payments.FormError:
        raise NameRefused() from None
    if len(name) < NAME_MIN:
        raise NameRefused()
    return name


def max_members(settings: Settings, owner: User | None, now: datetime) -> int:
    paid = owner is not None and bal.plan_active(owner, now)
    return settings.org_max_paid if paid else settings.org_max_trial


def member_count(db: Session, org_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Membership).where(Membership.org_id == org_id)) or 0


def members(db: Session, org_id: int) -> list[tuple[Membership, User]]:
    return list(db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                           .where(Membership.org_id == org_id)
                           .order_by(Membership.role.desc(), Membership.created_at, Membership.id)).all())


# ---------- create, rename, leave, remove, delete ----------

def create_org(db: Session, user: User, name: str, now: datetime) -> Organization | None:
    """None when the user is already in a company (also when a parallel request just made one)."""
    if membership_of(db, user.id) is not None:
        return None
    org = Organization(public_id=secrets.token_urlsafe(18), name=name, owner_user_id=user.id, created_at=now)
    db.add(org)
    try:
        db.flush()
        db.add(Membership(org_id=org.id, user_id=user.id, role=OWNER, created_at=now))
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    log.info("company created")
    return org


def rename(db: Session, org: Organization, name: str) -> None:
    org.name = name
    db.commit()


def remove_member(db: Session, org: Organization, membership_id: int) -> bool:
    """Owner only (checked by the caller). The owner's own row cannot be removed this way."""
    n = db.execute(delete(Membership).where(Membership.id == membership_id, Membership.org_id == org.id,
                                            Membership.role == MEMBER)).rowcount
    db.commit()
    if n:
        log.info("member removed")
    return n == 1


def remove_user(db: Session, user_id: int) -> bool:
    """Admin: take a member (never an owner) out of their company."""
    n = db.execute(delete(Membership).where(Membership.user_id == user_id, Membership.role == MEMBER)).rowcount
    db.commit()
    return n == 1


def leave(db: Session, user: User) -> str:
    """ok, owner (the owner cannot leave: remove the members or delete the company), none."""
    m = membership_of(db, user.id)
    if m is None:
        return "none"
    if m.role == OWNER:
        return "owner"
    db.execute(delete(Membership).where(Membership.id == m.id))
    db.commit()
    log.info("member left")
    return "ok"


def active_company_batch(db: Session, org_id: int) -> Batch | None:
    return db.scalar(select(Batch).where(Batch.org_id == org_id, Batch.status.in_(ACTIVE)).limit(1))


def delete_org(db: Session, org: Organization) -> str:
    """ok, or active (a company batch is running). Company batches go back to their authors as personal ones."""
    if active_company_batch(db, org.id) is not None:
        return "active"
    _drop_org(db, org.id)
    db.commit()
    log.info("company deleted")
    return "ok"


def _drop_org(db: Session, org_id: int) -> None:
    """Does not commit (also used by web/accounts.delete_user)."""
    db.execute(update(Batch).where(Batch.org_id == org_id).values(org_id=None))
    db.execute(delete(Invitation).where(Invitation.org_id == org_id))
    db.execute(delete(Membership).where(Membership.org_id == org_id))
    db.execute(delete(Organization).where(Organization.id == org_id))


def account_delete_refusal(db: Session, user: User) -> str | None:
    """Why the account cannot be deleted now because of a company: org_members (an owner with other people in the
    company) or wallet_active (a batch paid from this account is still running). None: it can."""
    m = membership_of(db, user.id)
    if m is not None and m.role == OWNER and member_count(db, m.org_id) > 1:
        return "org_members"
    if db.scalar(select(Batch.id).where(Batch.wallet_user_id == user.id, Batch.status.in_(ACTIVE)).limit(1)):
        return "wallet_active"
    return None


# ---------- money ----------

def wallet_reserved(db: Session, wallet_id: int) -> int:
    """Pages of the charged batches paid from this wallet that are queued or running (not charged yet)."""
    return db.scalar(select(func.coalesce(func.sum(Batch.pages_estimated), 0)).where(
        Batch.wallet_user_id == wallet_id, Batch.status.in_(ACTIVE), Batch.fmt != SCAN)) or 0


# ---------- who sees which batch ----------

def batch_scope(ctx: Ctx):
    """SQL condition for the batches this user may see (and delete). See the module docstring."""
    uid = ctx.user.id
    personal = and_(Batch.org_id.is_(None), Batch.user_id == uid)
    if ctx.membership is None:
        return personal
    if ctx.membership.role == OWNER:
        return or_(Batch.org_id == ctx.membership.org_id, personal)
    return and_(Batch.org_id == ctx.membership.org_id, Batch.user_id == uid)


def visible_batch(db: Session, ctx: Ctx, public_id: str) -> Batch | None:
    return db.scalar(select(Batch).where(Batch.public_id == public_id, batch_scope(ctx)))


def author_codes(db: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    return dict(db.execute(select(User.id, User.account_code).where(User.id.in_(user_ids))).all())


# ---------- invitations ----------

def _live(now: datetime):
    """Invitations that still hold a place: pending ones, and declined code invitations (they look the same as
    pending to the owner until they expire, so the owner cannot tell a refusal from a code that does not exist)."""
    return and_(Invitation.expires_at > now,
                or_(Invitation.status == "pending", and_(Invitation.kind == "id", Invitation.status == "declined")))


def housekeeping(db: Session, now: datetime) -> None:
    db.execute(update(Invitation).where(Invitation.status == "pending", Invitation.expires_at <= now)
               .values(status="expired"))
    db.execute(delete(Invitation).where(Invitation.expires_at < now - KEEP_INVITES))
    db.commit()


def live_for_org(db: Session, org_id: int, now: datetime) -> list[Invitation]:
    return list(db.scalars(select(Invitation).where(Invitation.org_id == org_id, _live(now))
                           .order_by(Invitation.created_at, Invitation.id)).all())


def invites_for_user(db: Session, user_id: int, now: datetime) -> list[tuple[Invitation, Organization]]:
    return list(db.execute(select(Invitation, Organization).join(Organization, Organization.id == Invitation.org_id)
                           .where(Invitation.target_user_id == user_id, Invitation.kind == "id",
                                  Invitation.status == "pending", Invitation.expires_at > now)
                           .order_by(Invitation.created_at, Invitation.id)).all())


def _limit(db: Session, settings: Settings, org: Organization, owner: User, now: datetime) -> str | None:
    if member_count(db, org.id) >= max_members(settings, owner, now):
        return "full"
    live = db.scalar(select(func.count()).select_from(Invitation).where(Invitation.org_id == org.id, _live(now))) or 0
    made = db.scalar(select(func.count()).select_from(RateEvent).where(
        RateEvent.bucket == BUCKET_INVITE, RateEvent.key_fp == bal.user_key(owner.id),
        RateEvent.created_at > now - DAY)) or 0
    if live >= LIVE_PER_ORG or made >= NEW_PER_OWNER_DAY:
        return "many"
    return None


def create_link(db: Session, settings: Settings, org: Organization, owner: User, now: datetime) -> tuple[str | None, str | None]:
    """(token, None) or (None, refusal code: full, many). The token is shown once; only its hash is stored."""
    refused = _limit(db, settings, org, owner, now)
    if refused:
        return None, refused
    token = secrets.token_urlsafe(24)
    db.add(Invitation(org_id=org.id, kind="link", token_hash=sec.token_hash(token), created_by=owner.id,
                      expires_at=now + INVITE_TTL, status="pending", created_at=now))
    db.add(RateEvent(bucket=BUCKET_INVITE, key_fp=bal.user_key(owner.id), created_at=now))
    db.commit()
    log.info("invitation created: link")
    return token, None


def create_by_code(db: Session, settings: Settings, org: Organization, owner: User, raw_code: str,
                   now: datetime) -> str | None:
    """None: the neutral "if such an account exists, the invitation was sent". Else a refusal code that never
    depends on the person behind the code: bad_code (not of the form SK-XXXXXX), full, many."""
    code = accounts.clean_account_code(raw_code)
    if code is None:
        return "bad_code"
    refused = _limit(db, settings, org, owner, now)
    if refused:
        return refused
    db.add(RateEvent(bucket=BUCKET_INVITE, key_fp=bal.user_key(owner.id), created_at=now))
    if db.scalar(select(Invitation.id).where(Invitation.org_id == org.id, Invitation.kind == "id",
                                             Invitation.target_code == code, _live(now)).limit(1)):
        db.commit()  # already in the list: same reply, no second row
        return None
    target = accounts.by_code(db, code)
    deliver = (
        target is not None and target.id != owner.id and target.blocked_at is None
        and db.scalar(select(Membership.id).where(Membership.user_id == target.id,
                                                  Membership.org_id == org.id)) is None
        and _live_for_user(db, target.id, now) < LIVE_PER_USER
    )
    inv = Invitation(org_id=org.id, kind="id", target_user_id=target.id if deliver else None, target_code=code,
                     created_by=owner.id, expires_at=now + INVITE_TTL, status="pending", created_at=now)
    db.add(inv)
    db.commit()
    if deliver and (_live_for_user(db, target.id, now) > LIVE_PER_USER or db.scalar(
            select(func.count()).select_from(Invitation).where(
                Invitation.org_id == org.id, Invitation.target_user_id == target.id, Invitation.status == "pending",
                Invitation.expires_at > now)) > 1):
        inv.target_user_id = None  # a parallel request delivered first: keep the owner's row, do not deliver twice
        db.commit()
    log.info("invitation created: code")
    return None


def _live_for_user(db: Session, user_id: int, now: datetime) -> int:
    return db.scalar(select(func.count()).select_from(Invitation).where(
        Invitation.target_user_id == user_id, Invitation.status == "pending", Invitation.expires_at > now)) or 0


def link_invite(db: Session, token: str, now: datetime) -> Invitation | None:
    """A live link invitation by its token, or None (unknown, used, declined, cancelled or expired)."""
    return db.scalar(select(Invitation).where(Invitation.token_hash == sec.token_hash(token), Invitation.kind == "link",
                                              Invitation.status == "pending", Invitation.expires_at > now))


def code_invite(db: Session, user: User, invite_id: int, now: datetime) -> Invitation | None:
    """A live code invitation addressed to this user, or None."""
    return db.scalar(select(Invitation).where(Invitation.id == invite_id, Invitation.kind == "id",
                                              Invitation.target_user_id == user.id, Invitation.status == "pending",
                                              Invitation.expires_at > now))


def accept(db: Session, settings: Settings, user: User, inv: Invitation, now: datetime) -> str:
    """ok, already (in a company), full, gone (the invitation was used, cancelled or expired meanwhile)."""
    if membership_of(db, user.id) is not None:
        return "already"
    org = db.get(Organization, inv.org_id)
    if org is None:
        return "gone"
    owner = db.get(User, org.owner_user_id)
    if member_count(db, org.id) >= max_members(settings, owner, now):
        return "full"
    done = db.execute(update(Invitation).where(Invitation.id == inv.id, Invitation.status == "pending",
                                               Invitation.expires_at > now).values(status="accepted"))
    if done.rowcount != 1:
        db.rollback()
        return "gone"
    db.add(Membership(org_id=org.id, user_id=user.id, role=MEMBER, created_at=now))
    try:
        db.commit()  # the invitation is used and the membership exists, together
    except IntegrityError:  # joined another company at the same moment
        db.rollback()
        return "already"
    if member_count(db, org.id) > max_members(settings, owner, now):  # parallel accepts: the last seat went twice
        db.execute(delete(Membership).where(Membership.user_id == user.id, Membership.org_id == org.id))
        db.execute(update(Invitation).where(Invitation.id == inv.id).values(status="pending"))
        db.commit()
        return "full"
    log.info("member joined: %s", inv.kind)
    return "ok"


def decline(db: Session, inv: Invitation) -> None:
    """A declined link is closed for everyone (it was meant for this person); a declined code invitation stays in
    the owner's list as sent until it expires."""
    db.execute(update(Invitation).where(Invitation.id == inv.id, Invitation.status == "pending")
               .values(status="declined"))
    db.commit()
    log.info("invitation declined: %s", inv.kind)


def cancel(db: Session, org: Organization, invite_id: int, now: datetime) -> bool:
    n = db.execute(update(Invitation).where(Invitation.id == invite_id, Invitation.org_id == org.id, _live(now))
                   .values(status="cancelled")).rowcount
    db.commit()
    return n == 1
