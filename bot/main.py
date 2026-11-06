"""Telegram bot entry point.

    python -m bot.main

Flow: the user sends photos/PDFs → picks the document type → picks Excel or 1C → gets one file back.
Texts and the Excel language follow the user's Telegram language (Russian or English).
Files live only in memory until the answer is sent. Logs contain counts, never document contents.
"""

import asyncio
from datetime import date
import json
import logging
from pathlib import PurePath
import time

import httpx
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.billing import FREE_FAILURES_PER_DAY, Job, cost_units, settle
from bot.config import BotConfig, load
from bot import phone_verify as pv
from bot.pdf import build_pdf
from docstruc.digitize import digitize
from bot.processing import process_batch
from bot.quota import QuotaStore
from bot.session import (
    DOC_CHOICES, LONG_RECEIPT_MAX_PARTS, MAX_BATCH_BYTES, MAX_PDF_PAGES, MAX_TOTAL_BYTES, Batch, Sessions,
)
from bot.texts import files_language, language_of, t
from docstruc.core import files
from docstruc.llm import get_backend

log = logging.getLogger("docstruc.bot")

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
SECONDS_PER_DOC = 10
MIME_SUFFIX = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def type_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "BTN_RECEIPTS"), callback_data="type:receipt"),
         InlineKeyboardButton(t(lang, "BTN_INVOICES"), callback_data="type:invoice")],
        [InlineKeyboardButton(t(lang, "BTN_ACTS"), callback_data="type:act"),
         InlineKeyboardButton(t(lang, "BTN_WAYBILLS"), callback_data="type:waybill")],
        [InlineKeyboardButton(t(lang, "BTN_LONG"), callback_data="type:receipt_long")],
        [InlineKeyboardButton(t(lang, "BTN_DIGITIZE"), callback_data="digitize")],
        [InlineKeyboardButton(t(lang, "BTN_PDF"), callback_data="pdf")],
        [InlineKeyboardButton(t(lang, "BTN_CLEAR"), callback_data="clear")],
    ])


def format_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "BTN_EXCEL"), callback_data="fmt:xlsx"),
         InlineKeyboardButton(t(lang, "BTN_1C"), callback_data="fmt:1c")],
        [InlineKeyboardButton(t(lang, "BTN_CLEAR"), callback_data="clear")],
    ])


def feedback_keyboard(lang: str, kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t(lang, "BTN_FB_GOOD"), callback_data=f"fb:good:{kind}"),
        InlineKeyboardButton(t(lang, "BTN_FB_BAD"), callback_data=f"fb:bad:{kind}"),
    ]])


FEEDBACK_MAX_CHARS = 1000
FEEDBACK_KINDS = {*(choice[0] for choice in DOC_CHOICES.values()), "digitize"}
FEEDBACK_REPORTS_PER_DAY = 3  # mistake reports forwarded to the owner per user and day
FREE_SCAN_PAGES_PER_DAY = 100
HEAVY_JOBS = 2  # batches processed at the same time; others wait (protects memory and CPU)


PHONE_PENDING_KEY = "phone_verify"  # user_data: (token, monotonic deadline) of a "/start v_<token>" flow
PHONE_PENDING_SECONDS = 10 * 60  # the website's link lives 15 minutes
PHONE_TIMEOUT = 5.0
UNAVAILABLE = "unavailable"
PHONE_REPLIES = {pv.OK: "PHONE_OK", pv.PHONE_USED: "PHONE_USED", pv.OTHER_PHONE: "PHONE_OTHER",
                 pv.INVALID: "PHONE_EXPIRED", UNAVAILABLE: "PHONE_UNAVAILABLE"}


async def post_phone_verified(base_url: str, secret: str, token: str, fp: str, *, transport=None,
                              now: float | None = None) -> str:
    """Tell the website that the number with this fingerprint was confirmed for this link token. One try, 5 s.
    Sends only the token, the fingerprint and the time, signed. Returns a status of bot/phone_verify.py or
    "unavailable" (network error, refusal, anything unexpected). Nothing about the request is logged."""
    ts = int(time.time() if now is None else now)
    headers = {"Content-Type": "application/json", pv.SIGNATURE_HEADER: pv.sign(secret, token, fp, ts)}
    body = json.dumps({"token": token, "phone_fp": fp, "ts": ts})
    try:
        async with httpx.AsyncClient(timeout=PHONE_TIMEOUT, transport=transport, follow_redirects=False) as client:
            response = await client.post(base_url.rstrip("/") + pv.PATH, content=body, headers=headers)
        status = response.json().get("status")
    except (httpx.HTTPError, ValueError, AttributeError):
        return UNAVAILABLE
    return status if status in (pv.OK, pv.PHONE_USED, pv.OTHER_PHONE, pv.INVALID) else UNAVAILABLE


def lang_of(update: Update) -> str:
    return language_of(update.effective_user.language_code if update.effective_user else None)


class BotApp:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.quota = QuotaStore(cfg.db_path, cfg.free_docs, cfg.daily_doc_limit)
        self.sessions = Sessions()
        get_backend(model=self.cfg.model)  # fail at start-up if the recognition backend is not configured
        self.heavy = asyncio.Semaphore(HEAVY_JOBS)
        self._reports: dict[tuple[int, str], int] = {}

    # ---- helpers -------------------------------------------------------------

    def _buy_text(self, lang: str) -> str:
        if not self.cfg.owner_contact:
            return ""
        prices = f"\n{self.cfg.prices_text}" if self.cfg.prices_text else ""
        return t(lang, "BUY", contact=self.cfg.owner_contact, prices=prices)

    def _is_owner(self, update: Update) -> bool:
        return self.cfg.owner_id is not None and update.effective_user.id == self.cfg.owner_id

    async def _show_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE, batch: Batch) -> None:
        lang = lang_of(update)
        text = t(lang, "FILE_RECEIVED", count=len(batch.files))
        chat_id = update.effective_chat.id
        if batch.status_message_id is not None:
            try:
                await context.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=batch.status_message_id, reply_markup=type_keyboard(lang)
                )
                return
            except Exception:  # message too old or unchanged: send a fresh one
                pass
        message = await context.bot.send_message(chat_id, text, reply_markup=type_keyboard(lang))
        batch.status_message_id = message.message_id

    # ---- commands ------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = getattr(context, "args", None) or []
        if args and args[0].startswith(pv.START_PREFIX):  # a link from the website: confirm the phone
            await self._phone_start(update, context, args[0])
            return
        remaining = self.quota.remaining(update.effective_user.id)
        await update.message.reply_text(t(lang_of(update), "WELCOME", free=self.cfg.free_docs, remaining=remaining))

    async def balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lang = lang_of(update)
        remaining = self.quota.remaining(update.effective_user.id)
        await update.message.reply_text(t(lang, "BALANCE", remaining=remaining) + self._buy_text(lang))

    def _prices_list(self, lang: str) -> str:
        """BOT_PRICES_TEXT is one line separated by ' · '; shown as a bulleted list."""
        if not self.cfg.prices_text:
            return t(lang, "PLANS_NO_PRICES")
        items = "\n".join(f"• {item.strip()}" for item in self.cfg.prices_text.split("·") if item.strip())
        return f"{items}\n\n{t(lang, 'PRICING_RULE')}"

    async def plans(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lang = lang_of(update)
        user_id = update.effective_user.id
        self.quota.event(user_id, "plans_viewed")
        await update.message.reply_text(t(
            lang, "PLANS",
            prices=self._prices_list(lang),
            contact=self.cfg.owner_contact or t(lang, "PLANS_NO_CONTACT"),
            user_id=user_id,
            remaining=self.quota.remaining(user_id),
        ))

    async def privacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(t(lang_of(update), "PRIVACY"))

    async def my_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(t(lang_of(update), "YOUR_ID", user_id=update.effective_user.id))

    async def grant(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        lang = lang_of(update)
        try:
            user_id, docs = int(context.args[0]), int(context.args[1])
            if not 0 < docs <= 100_000:
                raise ValueError
        except (IndexError, ValueError):
            await update.message.reply_text(t(lang, "GRANT_USAGE"))
            return
        remaining = self.quota.grant(user_id, docs)
        await update.message.reply_text(t(lang, "GRANTED", docs=docs, user_id=user_id, remaining=remaining))
        try:
            # The recipient's language is unknown here; Russian is the bot's main audience.
            await context.bot.send_message(user_id, t("ru", "GRANT_NOTICE", docs=docs, remaining=remaining))
        except Exception:
            log.info("could not notify user about granted documents")

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        await update.message.reply_text(
            t(lang_of(update), "STATS", **self.quota.stats(), daily_limit=self.cfg.daily_doc_limit)
        )

    # ---- phone confirmation for the website trial -------------------------------

    def _phone_ready(self) -> bool:
        return pv.valid_secret(getattr(self.cfg, "phone_secret", ""))

    async def _phone_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
        lang = lang_of(update)
        if not self._phone_ready():
            await update.message.reply_text(t(lang, "PHONE_OFF"))
            return
        token = pv.token_from_start(arg)
        if token is None:
            await update.message.reply_text(t(lang, "PHONE_EXPIRED"))
            return
        context.user_data[PHONE_PENDING_KEY] = (token, time.monotonic() + PHONE_PENDING_SECONDS)
        keyboard = ReplyKeyboardMarkup([[KeyboardButton(t(lang, "BTN_SHARE_PHONE"), request_contact=True)]],
                                       resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(t(lang, "PHONE_ASK"), reply_markup=keyboard)

    async def on_contact(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Only the user's own contact, sent with the button, during a pending website link. The number is turned
        into a fingerprint here and dropped; it is never stored, logged or sent."""
        message, user = update.message, update.effective_user
        pending = context.user_data.get(PHONE_PENDING_KEY) if context.user_data is not None else None
        if pending is None or message is None or message.contact is None or user is None:
            return  # a contact outside the website flow is ignored
        lang = lang_of(update)
        contact = message.contact
        if getattr(message, "forward_origin", None) is not None or contact.user_id is None \
                or contact.user_id != user.id:
            await message.reply_text(t(lang, "PHONE_NOT_OWN"))  # the button stays; the flow is still pending
            return
        context.user_data.pop(PHONE_PENDING_KEY, None)
        token, deadline = pending
        done = ReplyKeyboardRemove()
        if time.monotonic() > deadline:
            await message.reply_text(t(lang, "PHONE_EXPIRED"), reply_markup=done)
            return
        digits = pv.normalize_phone(contact.phone_number)
        if digits is None or not self._phone_ready():
            await message.reply_text(t(lang, "PHONE_UNAVAILABLE"), reply_markup=done)
            return
        fp = pv.phone_fp(self.cfg.phone_secret, digits)
        del digits
        status = await self._post_phone(token, fp)
        log.info("phone confirmation: %s", status)  # the outcome word only
        await message.reply_text(t(lang, PHONE_REPLIES.get(status, "PHONE_UNAVAILABLE")), reply_markup=done)

    async def _post_phone(self, token: str, fp: str) -> str:
        return await post_phone_verified(self.cfg.web_internal_url, self.cfg.phone_secret, token, fp)

    # ---- files ---------------------------------------------------------------

    async def on_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        lang = lang_of(update)
        batch = self.sessions.get(update.effective_user.id)
        if len(batch.files) >= self.cfg.max_files_per_batch:
            await message.reply_text(t(lang, "TOO_MANY_FILES", limit=self.cfg.max_files_per_batch))
            return

        if message.photo:
            tg_file_ref, name, size = message.photo[-1], f"photo_{len(batch.files) + 1}.jpg", message.photo[-1].file_size
        else:
            doc = message.document
            name = doc.file_name or f"file_{len(batch.files) + 1}{MIME_SUFFIX.get(doc.mime_type or '', '')}"
            if PurePath(name).suffix.lower() in {".heic", ".heif"}:
                await message.reply_text(t(lang, "HEIC_FILE"))
                return
            tg_file_ref, size = doc, doc.file_size

        if size and size > TELEGRAM_DOWNLOAD_LIMIT:
            await message.reply_text(t(lang, "TOO_LARGE"))
            return
        incoming = size or TELEGRAM_DOWNLOAD_LIMIT
        if batch.size + incoming > MAX_BATCH_BYTES:
            await message.reply_text(t(lang, "TOO_MUCH_DATA"))
            return
        if self.sessions.total_size() + incoming > MAX_TOTAL_BYTES:
            await message.reply_text(t(lang, "SERVER_BUSY"))
            return
        tg_file = await tg_file_ref.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        try:
            document = files.from_bytes(data, name)
            pages = files.page_count(document)
        except files.UnsupportedFileError as exc:
            await message.reply_text(t(lang, "UNSUPPORTED_FILE", reason=str(exc)))
            return
        if pages > MAX_PDF_PAGES:
            await message.reply_text(t(lang, "PDF_TOO_MANY_PAGES", pages=pages, limit=MAX_PDF_PAGES))
            return

        batch.files.append(document)
        batch.choice = None
        await self._show_status(update, context, batch)

    # ---- buttons -------------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        lang = lang_of(update)
        batch = self.sessions.get(update.effective_user.id)
        action, _, value = (query.data or "").partition(":")

        if action == "fb":
            await self._on_feedback_button(update, context, batch, value)
            return
        if action == "clear":
            batch.clear()
            await query.edit_message_text(t(lang, "CLEARED"))
            return
        if not batch.files:
            await query.edit_message_text(t(lang, "NOTHING_TO_PROCESS"))
            return
        if action == "pdf":
            await self._make_pdf(update, context, batch)
            return
        if action == "digitize":
            await self._digitize(update, context, batch)
            return
        if action == "type" and value in DOC_CHOICES:
            if DOC_CHOICES[value][1] and batch.pages > LONG_RECEIPT_MAX_PARTS:
                await query.edit_message_text(
                    t(lang, "LONG_TOO_MANY_PARTS", limit=LONG_RECEIPT_MAX_PARTS, count=batch.pages),
                    reply_markup=type_keyboard(lang),
                )
                return
            batch.choice = value
            await query.edit_message_text(t(lang, "CHOOSE_FORMAT", docs=batch.doc_count), reply_markup=format_keyboard(lang))
            return
        if action == "fmt" and value in ("xlsx", "1c") and batch.choice:
            await self._process(update, context, batch, value)

    def _charge(self, user_id: int, jobs: list[Job]) -> int:
        """Take documents from the allowance by the rules in bot/billing.py. Returns how many were taken."""
        free_left = max(0, FREE_FAILURES_PER_DAY - self.quota.free_failures_used(user_id))
        bill = settle(jobs, free_left)
        if bill.charged > 0:
            self.quota.consume(user_id, bill.charged)
        self.quota.add_daily(bill.work_units - bill.charged)  # all spending counts toward the daily cap
        self.quota.add_free_failures(user_id, bill.free_failures_used)
        log.info("charged=%d work=%d free_failures=%d", bill.charged, bill.work_units, bill.free_failures_used)
        return bill.charged

    # ---- feedback --------------------------------------------------------------

    async def _on_feedback_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE, batch: Batch, value: str) -> None:
        query = update.callback_query
        lang = lang_of(update)
        verdict, _, kind = value.partition(":")
        if verdict not in ("good", "bad") or kind not in FEEDBACK_KINDS:
            return  # buttons can be forged by modified clients: accept only what the bot itself sends
        self.quota.feedback(kind or "unknown", verdict)
        try:
            await query.edit_message_reply_markup(reply_markup=None)  # one vote per result
        except Exception:
            pass
        if verdict == "good":
            await context.bot.send_message(update.effective_chat.id, t(lang, "FB_THANKS"))
            return
        batch.feedback_for = kind or "unknown"
        await context.bot.send_message(update.effective_chat.id, t(lang, "FB_ASK"))

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A text message is only expected right after 👎: it describes the mistake and goes to the owner."""
        batch = self.sessions.get(update.effective_user.id)
        if batch.feedback_for is None:
            return
        kind, batch.feedback_for = batch.feedback_for, None
        lang = lang_of(update)
        user = update.effective_user
        key = (user.id, date.today().isoformat())
        self._reports = {k: v for k, v in self._reports.items() if k[1] == key[1]}  # keep today only
        self._reports[key] = self._reports.get(key, 0) + 1
        if self._reports[key] > FEEDBACK_REPORTS_PER_DAY:
            await update.message.reply_text(t(lang, "FB_RECEIVED"))
            return
        if self.cfg.owner_id is not None:
            who = f"@{user.username}" if user.username else user.full_name
            report = (
                f"👎 Problem with a result ({kind})\n"
                f"From: {who}, ID {user.id}, language {user.language_code or '?'}\n\n"
                f"{update.message.text[:FEEDBACK_MAX_CHARS]}"
            )
            try:
                await context.bot.send_message(self.cfg.owner_id, report)
            except Exception as exc:
                log.error("could not forward feedback to the owner: %s", type(exc).__name__)
        log.info("feedback received: kind=%s", kind)
        await update.message.reply_text(t(lang, "FB_RECEIVED"))

    async def _make_pdf(self, update: Update, context: ContextTypes.DEFAULT_TYPE, batch: Batch) -> None:
        query = update.callback_query
        lang = lang_of(update)
        if batch.lock.locked():
            await query.edit_message_text(t(lang, "BUSY"))
            return
        async with batch.lock:
            user_id = update.effective_user.id
            left = max(0, FREE_SCAN_PAGES_PER_DAY - self.quota.scan_pages_used(user_id))
            if batch.pages > left:
                await query.edit_message_text(
                    t(lang, "SCAN_DAILY_LIMIT", limit=FREE_SCAN_PAGES_PER_DAY, left=left), reply_markup=type_keyboard(lang)
                )
                return
            self.quota.add_scan_pages(user_id, batch.pages)
            files_to_merge = list(batch.files)
            batch.clear()
            await query.edit_message_text(t(lang, "PDF_PROCESSING", count=len(files_to_merge)))
            try:
                async with self.heavy:
                    content, pages = await asyncio.to_thread(build_pdf, files_to_merge)
            except Exception as exc:
                log.error("pdf build failed: %s", type(exc).__name__)
                await context.bot.send_message(update.effective_chat.id, t(lang, "ERROR"))
                return
            finally:
                files_to_merge.clear()
            filename = "documents.pdf" if lang == "en" else "документы.pdf"
            await context.bot.send_document(
                update.effective_chat.id, document=content, filename=filename,
                caption=t(lang, "PDF_DONE", count=pages),
            )
            log.info("built pdf: pages=%d", pages)

    async def _digitize(self, update: Update, context: ContextTypes.DEFAULT_TYPE, batch: Batch) -> None:
        """Each photo is one page of the typed PDF and counts as one document of the allowance."""
        query = update.callback_query
        lang = lang_of(update)
        user_id = update.effective_user.id
        if batch.lock.locked():
            await query.edit_message_text(t(lang, "BUSY"))
            return
        async with batch.lock:
            needed = batch.pages  # one page of the typed PDF per photo or PDF page
            remaining = self.quota.remaining(user_id)
            if needed > remaining:
                self.quota.event(user_id, "quota_exhausted")
                await query.edit_message_text(
                    t(lang, "NO_QUOTA", needed=needed, remaining=remaining) + self._buy_text(lang),
                    reply_markup=type_keyboard(lang),
                )
                return
            if needed > self.quota.daily_remaining():
                await query.edit_message_text(t(lang, "DAILY_LIMIT"))
                return
            pages_in = list(batch.files)
            batch.clear()
            await query.edit_message_text(t(lang, "DIGITIZE_PROCESSING", count=needed, seconds=needed * 20))
            backend = get_backend(model=self.cfg.model)  # one per batch, so its cost is this batch's cost
            try:
                async with self.heavy:
                    content, pages = await asyncio.to_thread(digitize, pages_in, backend)
            except Exception as exc:
                log.error("digitize failed: %s", type(exc).__name__)  # never log document content
                spent = cost_units(getattr(backend, "cost_usd", None))
                self._charge(user_id, [Job(units=0, cost_units=spent, ok=False)])
                await context.bot.send_message(update.effective_chat.id, t(lang, "ERROR"))
                return
            finally:
                pages_in.clear()
            self._charge(user_id, [Job(units=needed, cost_units=cost_units(getattr(backend, "cost_usd", None)), ok=True)])
            filename = "document.pdf" if lang == "en" else "документ.pdf"
            await context.bot.send_document(
                update.effective_chat.id, document=content, filename=filename,
                caption=t(lang, "DIGITIZE_DONE", pages=pages), reply_markup=feedback_keyboard(lang, "digitize"),
            )
            log.info("digitized: sources=%d pages=%d", needed, pages)

    async def _process(self, update: Update, context: ContextTypes.DEFAULT_TYPE, batch: Batch, fmt: str) -> None:
        query = update.callback_query
        lang = lang_of(update)
        user_id = update.effective_user.id
        if batch.lock.locked():
            await query.edit_message_text(t(lang, "BUSY"))
            return
        async with batch.lock:
            needed = batch.doc_count
            remaining = self.quota.remaining(user_id)
            if needed > remaining:
                self.quota.event(user_id, "quota_exhausted")
                await query.edit_message_text(
                    t(lang, "NO_QUOTA", needed=needed, remaining=remaining) + self._buy_text(lang),
                    reply_markup=format_keyboard(lang),
                )
                return
            if needed > self.quota.daily_remaining():
                await query.edit_message_text(t(lang, "DAILY_LIMIT"))
                return

            doctype, one_document = DOC_CHOICES[batch.choice]
            files_to_process = list(batch.files)
            batch.clear()
            await query.edit_message_text(t(lang, "PROCESSING", docs=needed, seconds=needed * SECONDS_PER_DOC))
            backend = get_backend(model=self.cfg.model)  # one per batch, so its cost is this batch's cost
            try:
                async with self.heavy:
                    output = await asyncio.to_thread(
                        process_batch, files_to_process, doctype, fmt, backend, one_document, files_language(lang)
                    )
            except Exception as exc:
                log.error("batch processing failed: %s", type(exc).__name__)  # never log document content
                await context.bot.send_message(update.effective_chat.id, t(lang, "ERROR"))
                return
            finally:
                files_to_process.clear()  # drop document bytes as soon as possible

            charged = self._charge(user_id, output.jobs)
            caption = t(lang, "DONE", docs=output.documents, review=output.needs_review)
            if output.not_found:
                caption += t(lang, "DONE_NOT_FOUND", count=output.not_found)
            if output.failed:
                caption += t(lang, "DONE_FAILED", failed=output.failed)
            caption += t(lang, "CHARGED", charged=charged)
            await context.bot.send_document(
                update.effective_chat.id, document=output.content, filename=output.filename, caption=caption,
                reply_markup=feedback_keyboard(lang, doctype),
            )
            log.info("processed batch: docs=%d review=%d failed=%d", output.documents, output.needs_review, output.failed)


def build_application(cfg: BotConfig) -> Application:
    bot = BotApp(cfg)
    app = Application.builder().token(cfg.token).concurrent_updates(True).build()
    private = filters.ChatType.PRIVATE  # the bot works in private chats only, never in groups
    app.add_handler(CommandHandler(["start", "help"], bot.start, filters=private))
    app.add_handler(CommandHandler("balance", bot.balance, filters=private))
    app.add_handler(CommandHandler(["plans", "buy"], bot.plans, filters=private))
    app.add_handler(CommandHandler("id", bot.my_id, filters=private))
    app.add_handler(CommandHandler("privacy", bot.privacy, filters=private))
    app.add_handler(CommandHandler("grant", bot.grant, filters=private))
    app.add_handler(CommandHandler("stats", bot.stats, filters=private))
    app.add_handler(MessageHandler(private & (filters.PHOTO | filters.Document.ALL), bot.on_file))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, bot.on_text))
    app.add_handler(MessageHandler(private & filters.CONTACT, bot.on_contact))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request logs contain the bot token in URLs
    build_application(load()).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
