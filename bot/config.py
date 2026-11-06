"""Bot settings from environment variables (.env)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from docstruc import config as _engine_config  # noqa: F401  (loads .env)


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class BotConfig:
    token: str
    owner_id: int | None
    owner_contact: str
    prices_text: str
    free_docs: int
    daily_doc_limit: int
    max_files_per_batch: int
    db_path: Path
    model: str
    # Phone confirmation for the website trial (bot/phone_verify.py). Empty secret: the bot says it is unavailable.
    phone_secret: str = field(default="", repr=False)
    web_internal_url: str = "http://127.0.0.1:8000"


def load() -> BotConfig:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Put it in the .env file (see .env.example).")
    owner = os.environ.get("BOT_OWNER_ID", "").strip()
    if owner and not owner.isdigit():
        raise RuntimeError(
            "BOT_OWNER_ID must be your numeric Telegram id (send /id to the bot), "
            "not a username. Put the @username into BOT_OWNER_CONTACT."
        )
    phone_secret = os.environ.get("PHONE_VERIFY_SECRET", "").strip()
    if phone_secret and len(phone_secret) < 32:
        raise RuntimeError("PHONE_VERIFY_SECRET must have 32+ characters (the same value as on the website)")
    web_url = os.environ.get("WEB_INTERNAL_URL", "").strip() or "http://127.0.0.1:8000"
    if not web_url.startswith(("http://", "https://")):
        raise RuntimeError("WEB_INTERNAL_URL must start with http:// or https://")
    return BotConfig(
        token=token,
        owner_id=int(owner) if owner else None,
        owner_contact=os.environ.get("BOT_OWNER_CONTACT", "").strip(),
        prices_text=os.environ.get("BOT_PRICES_TEXT", "").strip(),
        free_docs=_int("BOT_FREE_DOCS", 0),
        daily_doc_limit=_int("BOT_DAILY_DOC_LIMIT", 200),
        max_files_per_batch=_int("BOT_MAX_FILES", 30),
        db_path=Path(os.environ.get("BOT_DB_PATH", "data/bot.sqlite3")),
        # The Telegram bot is the low-price product: Sonnet (≈99.7% on our receipts, no silent errors,
        # ≈2× cheaper than Opus). Client orders and business plans use the best model (Opus).
        model=os.environ.get("BOT_MODEL", "claude-sonnet-5-5").strip(),
        phone_secret=phone_secret,
        web_internal_url=web_url,
    )
