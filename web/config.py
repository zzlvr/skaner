"""Website settings, read from environment (.env). No secrets in code."""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

LANGS = ("en", "ru", "kk")
DEFAULT_LANG = "en"  # the main language of the product; ru and kk are translations

# Purposes for keys derived from WEB_SECRET: one key per purpose, never reused.
KEY_LABELS = ("fp:email", "fp:ip", "fp:ua", "code", "cookie", "csrf", "results", "requests")

MB = 1024 * 1024
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def default_db_url() -> str:
    return os.environ.get("WEB_DB_URL", "sqlite:///" + str(Path("data/web.sqlite3")))


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@lru_cache(maxsize=64)
def _derive(secret: str, label: str) -> bytes:
    """HKDF-SHA256 (RFC 5869) with a fixed salt; the label is the HKDF 'info'. One 32-byte block."""
    if label not in KEY_LABELS:
        raise ValueError(f"unknown key label: {label}")
    prk = hmac.new(b"skaner-web/v1", secret.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, label.encode("ascii") + b"\x01", hashlib.sha256).digest()


@dataclass(frozen=True)
class Settings:
    db_url: str
    secret: str = field(repr=False)  # root secret; only derived keys are used (see key())
    base_url: str
    dev: bool
    # Mail provider (Resend HTTP API). Empty key: console mailer in dev, refuse to start in production.
    resend_api_key: str = field(default="", repr=False)
    mail_from: str = ""  # e.g. "Skaner <login@skaner.app>"; the domain must be verified at the provider
    # Recognition: always the paid Claude API (client documents never go to a free tier), Opus by default.
    model: str = "claude-opus-5-5"
    # Money and capacity limits (safe defaults; each can be changed in .env, see load()).
    daily_pages: int = 300  # pages of work per UTC day across all website users
    # Part of daily_pages that batches paid only by trial pages may use per UTC day, so trial farming (many
    # accounts) can never starve paying customers. Clamped to 0..daily_pages (see __post_init__).
    trial_daily_pages: int = 100
    batches_per_hour: int = 20  # per user
    max_files: int = 20  # per batch
    max_file_bytes: int = 20 * MB
    max_pdf_pages: int = 20
    max_batch_bytes: int = 60 * MB
    memory_total: int = 400 * MB  # uploaded bytes held in memory by all batches being read, queued or processed
    memory_user: int = 100 * MB
    workers: int = 2  # batches recognised at the same time; others wait in a queue
    trial_pages: int = 0  # no free trial (decision 67): 0 = none; the bring-your-own-key path is the free one
    trial_days: int = 14
    trials_per_ip_day: int = 3
    retention_days: int = 90
    # Free "PDF scan" (no recognition, runs on our CPU): pages per user per UTC day, pages per day for the
    # whole site (CPU protection, separate from the paid cap above), minutes the finished PDF can be downloaded.
    free_scan_pages_day: int = 100
    scan_pages_day: int = 2000
    scan_file_minutes: int = 60
    # Owner admin (/admin): raw addresses from WEB_ADMIN_EMAILS, kept only in memory; the app fingerprints them
    # at start-up and compares fingerprints. Nothing in the database marks a user as admin.
    admin_emails: tuple[str, ...] = field(default=(), repr=False)
    # Owner notification about payment requests (shared with the bot): TELEGRAM_BOT_TOKEN and BOT_OWNER_ID.
    telegram_token: str = field(default="", repr=False)
    telegram_owner_id: str = field(default="", repr=False)
    # LOCAL DEVELOPMENT ONLY: a fixed demo address with a permanent 6-digit code, so the owner can always look at the
    # cabinet as a client sees it. Refused outside WEB_DEV=1 (here and again in create_app).
    demo_email: str = ""
    demo_code: str = field(default="", repr=False)
    # One trial per phone number, confirmed in the Telegram bot (web/phone.py). Off by default. When on: the bot's
    # username (for the t.me link), the secret shared with the bot (32+ characters), and the pages given at once
    # (the rest of trial_pages comes after the phone is confirmed).
    phone_verify: bool = False
    bot_username: str = ""
    phone_verify_secret: str = field(default="", repr=False)
    trial_instant_pages: int = 0
    # Company cabinets (web/company.py): people per company, the owner included, without and with an active plan.
    org_max_trial: int = 5
    org_max_paid: int = 10
    # Bot protection on the login form (Cloudflare Turnstile, web/turnstile.py). Both set = on; neither = off (dev, tests).
    turnstile_site_key: str = ""
    pay_email: str = ""  # shown on the Plans page: where to write to pay (manual payment); empty: not shown
    turnstile_secret: str = field(default="", repr=False)
    # Login-code mails the whole site may send per hour / per UTC day (the mail provider's free plan is small: past
    # these numbers one person guessing addresses would cut everybody's sign-in off). The owner is told once a day.
    mail_hourly: int = 30
    mail_daily: int = 80

    def __post_init__(self) -> None:
        # The trial part can never be larger than the whole daily cap (nor negative).
        object.__setattr__(self, "trial_daily_pages", max(0, min(self.trial_daily_pages, self.daily_pages)))

    def key(self, label: str) -> bytes:
        return _derive(self.secret, label)

    @classmethod
    def load(cls) -> "Settings":
        dev = os.environ.get("WEB_DEV", "") == "1"
        secret = os.environ.get("WEB_SECRET", "")
        if len(secret) < 32:
            if not dev:
                raise RuntimeError("WEB_SECRET must be set (32+ characters) outside dev mode")
            secret = "dev-only-secret-not-for-production-use"
        demo_email = os.environ.get("WEB_DEMO_EMAIL", "").strip()
        demo_code = os.environ.get("WEB_DEMO_CODE", "").strip()
        if (demo_email or demo_code) and not dev:
            raise RuntimeError("WEB_DEMO_EMAIL / WEB_DEMO_CODE work only in local development (WEB_DEV=1)")
        base_url = os.environ.get("WEB_BASE_URL", "http://localhost:8000")
        if dev and urlsplit(base_url).hostname not in LOCAL_HOSTS:
            # WEB_DEV=1 turns off Secure cookies and HSTS, allows a built-in secret and the demo login: a server
            # with a public address must never run like that, even if the flag was copied there by mistake.
            raise RuntimeError("WEB_DEV=1 works only with a local WEB_BASE_URL (localhost / 127.0.0.1)")
        phone_verify = os.environ.get("WEB_PHONE_VERIFY", "").strip() == "1"
        bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
        phone_secret = os.environ.get("PHONE_VERIFY_SECRET", "").strip()
        settings = cls(
            db_url=default_db_url(),
            secret=secret,
            base_url=base_url,
            dev=dev,
            resend_api_key=os.environ.get("RESEND_API_KEY", ""),
            mail_from=os.environ.get("WEB_MAIL_FROM", ""),
            model=os.environ.get("WEB_MODEL", "").strip() or "claude-opus-5-5",
            daily_pages=_int("WEB_DAILY_PAGES", 300),
            trial_daily_pages=_int("WEB_TRIAL_DAILY_PAGES", 100),
            batches_per_hour=_int("WEB_BATCHES_PER_HOUR", 20),
            max_files=_int("WEB_MAX_FILES", 20),
            max_file_bytes=_int("WEB_MAX_FILE_MB", 20) * MB,
            max_pdf_pages=_int("WEB_MAX_PDF_PAGES", 20),
            max_batch_bytes=_int("WEB_MAX_BATCH_MB", 60) * MB,
            memory_total=_int("WEB_MEMORY_TOTAL_MB", 400) * MB,
            memory_user=_int("WEB_MEMORY_USER_MB", 100) * MB,
            workers=_int("WEB_WORKERS", 2),
            trial_pages=_int("WEB_TRIAL_PAGES", 0),
            trial_days=_int("WEB_TRIAL_DAYS", 14),
            trials_per_ip_day=_int("WEB_TRIALS_PER_IP_DAY", 3),
            retention_days=_int("WEB_RETENTION_DAYS", 90),
            free_scan_pages_day=_int("WEB_FREE_SCAN_PAGES_DAY", 100),
            scan_pages_day=_int("WEB_SCAN_PAGES_DAY", 2000),
            scan_file_minutes=_int("WEB_SCAN_FILE_MINUTES", 60),
            admin_emails=tuple(a.strip() for a in os.environ.get("WEB_ADMIN_EMAILS", "").split(",") if a.strip()),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_owner_id=os.environ.get("BOT_OWNER_ID", "").strip(),
            demo_email=demo_email,
            demo_code=demo_code,
            phone_verify=phone_verify,
            bot_username=bot_username,
            phone_verify_secret=phone_secret,
            trial_instant_pages=_int("WEB_TRIAL_INSTANT_PAGES", 0),
            org_max_trial=_int("WEB_ORG_MAX_TRIAL", 5),
            org_max_paid=_int("WEB_ORG_MAX_PAID", 10),
            turnstile_site_key=os.environ.get("TURNSTILE_SITE_KEY", "").strip(),
            pay_email=os.environ.get("WEB_PAY_EMAIL", "").strip(),
            turnstile_secret=os.environ.get("TURNSTILE_SECRET", "").strip(),
            mail_hourly=_int("WEB_MAIL_HOURLY", 30),
            mail_daily=_int("WEB_MAIL_DAILY", 80),
        )
        if bool(settings.turnstile_site_key) != bool(settings.turnstile_secret):
            raise RuntimeError("TURNSTILE_SITE_KEY and TURNSTILE_SECRET must be set together (or both left empty)")
        check_phone_settings(settings)
        return settings


BOT_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


def check_phone_settings(settings: Settings) -> None:
    """Refuse to start when phone confirmation is on but cannot work safely."""
    if not settings.phone_verify:
        return
    if not BOT_USERNAME_RE.fullmatch(settings.bot_username or ""):
        raise RuntimeError("WEB_PHONE_VERIFY=1 needs TELEGRAM_BOT_USERNAME (the bot's @username, without @)")
    if len(settings.phone_verify_secret or "") < 32:
        raise RuntimeError("WEB_PHONE_VERIFY=1 needs PHONE_VERIFY_SECRET (32+ characters, the same value for the bot)")
    if not 0 <= settings.trial_instant_pages <= settings.trial_pages:
        raise RuntimeError("WEB_TRIAL_INSTANT_PAGES must be between 0 and WEB_TRIAL_PAGES")
