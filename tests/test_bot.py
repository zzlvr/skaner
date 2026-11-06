from datetime import date

import pytest
from openpyxl import load_workbook
from io import BytesIO

from bot.processing import process_batch
from bot.quota import QuotaStore
from bot.session import Batch
from docstruc.core.models import Document


@pytest.fixture
def store():
    return QuotaStore(":memory:", free_docs=10, daily_limit=50)


def test_new_user_gets_free_documents(store):
    assert store.remaining(1) == 10


def test_consume_and_grant(store):
    store.consume(1, 7)
    assert store.remaining(1) == 3
    assert store.grant(1, 100) == 103
    store.consume(1, 103)
    assert store.remaining(1) == 0
    store.consume(1, 5)  # never negative
    assert store.remaining(1) == 0


def test_daily_limit_is_global_and_per_day(store):
    today, tomorrow = date(2026, 10, 18), date(2026, 10, 19)
    store.consume(1, 30, today=today)
    store.consume(2, 15, today=today)
    assert store.daily_remaining(today=today) == 5
    assert store.daily_remaining(today=tomorrow) == 50
    stats = store.stats(today=today)
    assert (stats["users"], stats["docs_total"], stats["docs_today"]) == (2, 45, 45)


def test_long_receipt_is_billed_by_photos():
    batch = Batch(files=[Document(name=f"p{i}.jpg", mime_type="image/jpeg", data=b"x") for i in range(4)])
    assert batch.doc_count == 4
    batch.choice = "receipt_long"
    assert batch.doc_count == 4  # we pay for every photo
    batch.clear()
    assert batch.doc_count == 0 and batch.choice is None


class FakeBackend:
    name = "fake"

    def extract(self, parts, schema, instructions):
        if parts[0].name == "broken.jpg":
            raise RuntimeError("boom")
        receipt = {"merchant_name": "Shop", "date": "2026-10-01", "total": 5,
                   "items": [{"description": "A", "quantity": 1, "unit_price": 5, "total": 5}]}
        if "documents" in schema.model_fields:  # extract_all asks for a list of documents
            return schema.model_validate({"documents": [receipt]})
        return schema.model_validate(receipt)


def docs(*names):
    return [Document(name=n, mime_type="image/jpeg", data=b"x") for n in names]


def test_process_batch_excel_and_failures():
    out = process_batch(docs("a.jpg", "broken.jpg"), "receipt", "xlsx", FakeBackend())
    assert (out.documents, out.failed, out.filename) == (2, 1, "documents.xlsx")
    wb = load_workbook(BytesIO(out.content))
    assert wb.sheetnames == ["Documents", "Line items", "Review"]


def test_process_batch_1c_and_long_receipt():
    out = process_batch(docs("p1.jpg", "p2.jpg"), "receipt", "1c", FakeBackend(), one_document=True)
    assert (out.documents, out.failed, out.filename) == (1, 0, "documents_1c.xlsx")
    assert load_workbook(BytesIO(out.content)).sheetnames[0] == "Товары и услуги"


def test_failed_document_does_not_leak_error_details():
    out = process_batch(docs("broken.jpg"), "receipt", "xlsx", FakeBackend())
    review = load_workbook(BytesIO(out.content))["Review"]
    messages = [row[3] for row in review.iter_rows(min_row=2, values_only=True)]
    assert messages == ["Extraction failed: RuntimeError"]


def test_process_batch_russian_excel():
    out = process_batch(docs("a.jpg"), "receipt", "xlsx", FakeBackend(), lang="ru")
    assert out.filename == "документы.xlsx"
    assert load_workbook(BytesIO(out.content)).sheetnames == ["Документы", "Позиции", "Проверка"]


def test_bot_language_follows_telegram_language():
    from bot.texts import TEXTS, language_of, t
    assert [language_of(c) for c in ("ru", "kk", "kk-KZ", "ru-RU", "uz", "en", "de", None)] == \
        ["ru", "kk", "kk", "ru", "ru", "en", "en", "en"]
    assert set(TEXTS["ru"]) == set(TEXTS["en"]) == set(TEXTS["kk"])  # every text exists in every language
    assert "10" in t("en", "WELCOME", free=10, remaining=10)


def test_build_pdf_from_photos_and_pdfs(tmp_path):
    import pymupdf
    from PIL import Image
    from bot.pdf import build_pdf

    photo = BytesIO()
    Image.new("RGB", (1200, 1800), "white").save(photo, "JPEG")
    source_pdf = pymupdf.open()
    source_pdf.new_page()
    source_pdf.new_page()
    pdf_bytes = source_pdf.tobytes()
    content, pages = build_pdf([
        Document(name="a.jpg", mime_type="image/jpeg", data=photo.getvalue()),
        Document(name="b.pdf", mime_type="application/pdf", data=pdf_bytes),
    ])
    assert pages == 3
    with pymupdf.open(stream=content, filetype="pdf") as out:
        assert out.page_count == 3
        first = out[0].rect
        assert first.height == 842 and abs(first.width - 842 * 1200 / 1800) < 1  # photo proportions kept


def test_privacy_text_is_truthful_without_naming_the_provider():
    from bot.texts import TEXTS, t
    for lang in TEXTS:
        text = t(lang, "PRIVACY")
        assert "Anthropic" not in text and "Claude" not in text and "@" not in text
        # Never claim that files are not shared at all: recognition does use an external service.
        assert "не передаются" not in text and "not shared" not in text
        # Never claim that everything is processed on our own server: recognition is external.
        assert "нашем сервере" not in text and "our server" not in text
    assert "не храним" in t("ru", "PRIVACY")


def test_plans_text_lists_prices_contact_and_user_id():
    from bot.texts import t
    prices = "\n".join(f"• {x.strip()}" for x in "30 документов — 1 990 ₸ · 100 документов — 4 990 ₸".split("·"))
    text = t("ru", "PLANS", prices=prices, contact="@owner", user_id=123, remaining=2)
    assert "• 30 документов — 1 990 ₸\n• 100 документов — 4 990 ₸" in text
    assert "@owner" in text and "123" in text and "Осталось у вас: 2" in text


def test_demand_events_count_users_not_repeats(store):
    store.remaining(1); store.remaining(2); store.remaining(3)
    store.event(1, "quota_exhausted"); store.event(1, "quota_exhausted")
    store.event(2, "quota_exhausted"); store.event(2, "plans_viewed")
    store.grant(3, 30)
    stats = store.stats()
    assert (stats["exhausted"], stats["plans_viewed"], stats["paid_users"], stats["new_today"]) == (2, 1, 1, 3)


def test_kazakh_users_get_russian_files():
    from bot.texts import files_language, t
    assert [files_language(l) for l in ("kk", "ru", "en")] == ["ru", "ru", "en"]
    for key in ("WELCOME", "PLANS"):  # formatting placeholders work in Kazakh too
        assert t("kk", key, free=5, remaining=3, prices="• 30", contact="@o", user_id=1)


def test_feedback_is_counted_anonymously(store):
    store.feedback("receipt", "good"); store.feedback("act", "good"); store.feedback("receipt", "bad")
    stats = store.stats()
    assert (stats["fb_good"], stats["fb_bad"]) == (2, 1)
    from bot.texts import TEXTS, t
    for lang in TEXTS:
        assert "👎 1" in t(lang, "STATS", **stats, daily_limit=50)


def _fake_bot_app(store, owner_id):
    from types import SimpleNamespace
    from bot.main import BotApp
    from bot.session import Sessions
    app = BotApp.__new__(BotApp)  # skip the real backend
    app.cfg, app.quota, app.sessions = SimpleNamespace(owner_id=owner_id), store, Sessions()
    app._reports = {}
    return app


def _text_update(text, replies):
    from types import SimpleNamespace

    async def reply_text(msg):
        replies.append(msg)

    user = SimpleNamespace(id=7, username="client", full_name="Client", language_code="ru")
    return SimpleNamespace(effective_user=user, message=SimpleNamespace(text=text, reply_text=reply_text))


def test_mistake_report_goes_to_owner_only_after_thumbs_down(store):
    import asyncio
    from types import SimpleNamespace
    sent, replies = [], []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    app = _fake_bot_app(store, owner_id=1)

    asyncio.run(app.on_text(_text_update("hello", replies), context))
    assert sent == [] and replies == []  # ordinary text is ignored

    app.sessions.get(7).feedback_for = "act"
    asyncio.run(app.on_text(_text_update("неверный БИН заказчика", replies), context))
    assert len(sent) == 1 and sent[0][0] == 1
    assert "(act)" in sent[0][1] and "@client" in sent[0][1] and "неверный БИН заказчика" in sent[0][1]
    assert len(replies) == 1 and app.sessions.get(7).feedback_for is None

    asyncio.run(app.on_text(_text_update("ещё", replies), context))
    assert len(sent) == 1  # only one message per 👎


def _pdf(pages):
    import pymupdf
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    return Document(name=f"scan{pages}.pdf", mime_type="application/pdf", data=doc.tobytes())


class CountingBackend(FakeBackend):
    """Returns two receipts for 'two.jpg', none for 'empty.jpg'; counts calls."""

    def __init__(self):
        self.calls = 0

    def extract(self, parts, schema, instructions):
        self.calls += 1
        if "documents" in schema.model_fields and parts[0].name in ("two.jpg", "empty.jpg"):
            receipt = super().extract(parts, schema, instructions).documents[0]
            return schema.model_validate({"documents": [] if parts[0].name == "empty.jpg" else [receipt, receipt]})
        return super().extract(parts, schema, instructions)


def test_pages_are_counted_and_split():
    from docstruc.core.files import page_count, split_pages
    assert page_count(_pdf(3)) == 3 and page_count(docs("a.jpg")[0]) == 1
    parts = split_pages(_pdf(3))
    assert [page_count(p) for p in parts] == [1, 1, 1] and parts[1].name == "scan3.pdf p2"


def test_billing_one_page_one_document():
    from bot.billing import settle
    out = process_batch([_pdf(3), *docs("two.jpg", "empty.jpg", "broken.jpg")], "receipt", "xlsx", CountingBackend())
    # PDF: 3 pages, one receipt → 3; photo with two receipts → 2; empty photo → 1 (work done); broken → free
    assert [(j.units, j.ok) for j in out.jobs] == [(3, True), (2, True), (1, True), (0, False)]
    assert (settle(out.jobs, free_failures_left=3).charged, out.failed, out.not_found) == (6, 1, 1)


def test_long_receipt_counts_by_photos_and_batch_counts_pdf_pages():
    from bot.billing import settle
    out = process_batch(docs("p1.jpg", "p2.jpg", "p3.jpg"), "receipt", "xlsx", CountingBackend(), one_document=True)
    assert out.documents == 1 and settle(out.jobs, 3).charged == 3
    batch = Batch(files=[_pdf(4), *docs("a.jpg")])
    assert batch.pages == 5 and batch.doc_count == 5


def test_free_pages_still_count_toward_the_daily_cap(store):
    store.add_daily(3)
    assert store.daily_remaining() == 47


def test_digitize_transcribes_a_pdf_page_by_page():
    from docstruc.digitize import DigitalPage, digitize

    class PageBackend:
        name = "fake"
        calls = 0

        def extract(self, parts, schema, instructions):
            PageBackend.calls += 1
            return DigitalPage(html="<p>text</p>")

    _, pages = digitize([_pdf(3), *docs("a.jpg")], PageBackend())
    assert PageBackend.calls == 4 and pages == 4


def test_pricing_rule_is_shown_in_every_language():
    from bot.texts import TEXTS, t
    for lang in TEXTS:
        assert "1" in t(lang, "PRICING_RULE") and "2–3" in t(lang, "PRICING_RULE")


def test_charge_never_below_actual_cost():
    from bot.billing import Job, cost_units, settle
    assert [cost_units(c) for c in (None, 0, 0.03, 0.09, 0.10, 0.20)] == [0, 0, 1, 1, 2, 3]
    # a dense page that cost $0.20 (or needed a retry) is charged 3 instead of 1
    assert settle([Job(units=1, cost_units=3, ok=True)], 3).charged == 3


def test_failures_free_only_when_they_cost_nothing_or_within_daily_allowance():
    from bot.billing import Job, settle
    outage = Job(units=0, cost_units=0, ok=False)  # the service did not answer: we paid nothing
    paid_failure = Job(units=0, cost_units=2, ok=False)
    assert settle([outage] * 5, 0).charged == 0
    bill = settle([paid_failure] * 4, free_failures_left=3)
    assert (bill.charged, bill.free_failures_used, bill.work_units) == (2, 3, 8)


def test_cost_is_measured_per_file(monkeypatch):
    class PricedBackend(CountingBackend):
        cost_usd = 0.0

        def extract(self, parts, schema, instructions):
            PricedBackend.cost_usd += 0.20 if parts[0].name == "dense.jpg" else 0.02
            return super().extract(parts, schema, instructions)

    out = process_batch(docs("a.jpg", "dense.jpg"), "receipt", "xlsx", PricedBackend())
    assert [(j.units, j.cost_units) for j in out.jobs] == [(1, 1), (1, 3)]


def test_free_failures_are_counted_per_user_and_day(store):
    from datetime import date
    d = date(2026, 10, 19)
    store.add_free_failures(1, 2, today=d)
    assert (store.free_failures_used(1, today=d), store.free_failures_used(2, today=d)) == (2, 0)
    assert store.free_failures_used(1, today=date(2026, 10, 20)) == 0


def test_output_budget_grows_with_pages():
    from docstruc.llm.claude import MAX_TOKENS, output_budget
    one, three = output_budget(docs("a.jpg")), output_budget([_pdf(3)])
    assert one < three <= MAX_TOKENS and output_budget([_pdf(20)]) == MAX_TOKENS


# ---------- phone confirmation for the website trial ----------
# Fictional numbers only (+1 202 555 01xx is reserved for fiction).

PHONE_SECRET = "phone-secret-" + "p" * 40
PHONE_TOKEN = "T" * 32


def _phone_bot(secret=PHONE_SECRET, status="ok"):
    from types import SimpleNamespace
    from bot.main import BotApp
    app = BotApp.__new__(BotApp)  # skip the real backend
    app.cfg = SimpleNamespace(owner_id=None, phone_secret=secret, web_internal_url="http://127.0.0.1:1",
                              free_docs=10)
    app.quota = QuotaStore(":memory:", free_docs=10, daily_limit=50)
    app.posted = []

    async def post(token, fp):
        app.posted.append((token, fp))
        return status

    app._post_phone = post
    return app


def _phone_update(lang="ru", user_id=7, contact_user_id=7, number="+1 202 555 0143", forwarded=False):
    from types import SimpleNamespace
    replies = []

    async def reply_text(text, reply_markup=None):
        replies.append((text, reply_markup))

    user = SimpleNamespace(id=user_id, username="client", full_name="Client", language_code=lang)
    c = SimpleNamespace(user_id=contact_user_id, phone_number=number)
    msg = SimpleNamespace(text=None, contact=c, forward_origin=object() if forwarded else None, reply_text=reply_text)
    return SimpleNamespace(effective_user=user, message=msg), replies


def _ctx(args=None, user_data=None):
    from types import SimpleNamespace
    return SimpleNamespace(args=args or [], user_data={} if user_data is None else user_data)


def test_start_with_site_link_shows_the_contact_button():
    import asyncio
    from telegram import ReplyKeyboardMarkup
    from bot.main import PHONE_PENDING_KEY
    from bot.texts import t
    app, ctx = _phone_bot(), _ctx(["v_" + PHONE_TOKEN])
    upd, replies = _phone_update()
    asyncio.run(app.start(upd, ctx))
    text, markup = replies[0]
    assert text == t("ru", "PHONE_ASK") and isinstance(markup, ReplyKeyboardMarkup)
    button = markup.keyboard[0][0]
    assert button.request_contact is True and button.text == t("ru", "BTN_SHARE_PHONE")
    assert ctx.user_data[PHONE_PENDING_KEY][0] == PHONE_TOKEN
    # A malformed token does not start a flow; a plain /start is the old welcome.
    ctx2 = _ctx(["v_bad token!"])
    upd, replies = _phone_update()
    asyncio.run(app.start(upd, ctx2))
    assert replies[0][0] == t("ru", "PHONE_EXPIRED") and ctx2.user_data == {}
    upd, replies = _phone_update()
    asyncio.run(app.start(upd, _ctx()))
    assert replies[0][0] == t("ru", "WELCOME", free=10, remaining=10)


def test_phone_flow_unavailable_without_configuration():
    import asyncio
    from bot.texts import t
    app, ctx = _phone_bot(secret=""), _ctx(["v_" + PHONE_TOKEN])
    for lang in ("ru", "kk", "en"):
        upd, replies = _phone_update(lang)
        asyncio.run(app.start(upd, ctx))
        assert replies == [(t(lang, "PHONE_OFF"), None)] and ctx.user_data == {}


def test_own_contact_sends_only_token_and_fingerprint(caplog):
    import asyncio
    import logging
    from telegram import ReplyKeyboardRemove
    from bot import phone_verify as pv
    from bot.main import PHONE_PENDING_KEY
    from bot.texts import t
    caplog.set_level(logging.DEBUG)
    app, ctx = _phone_bot(), _ctx(["v_" + PHONE_TOKEN])
    upd, _ = _phone_update()
    asyncio.run(app.start(upd, ctx))
    upd, replies = _phone_update()
    asyncio.run(app.on_contact(upd, ctx))
    assert app.posted == [(PHONE_TOKEN, pv.phone_fp(PHONE_SECRET, "12025550143"))]
    assert replies[0][0] == t("ru", "PHONE_OK") and isinstance(replies[0][1], ReplyKeyboardRemove)
    assert PHONE_PENDING_KEY not in ctx.user_data  # one use
    for r in caplog.records:
        m = r.getMessage() + repr(r.args)
        assert "12025550143" not in m and "555" not in m and PHONE_TOKEN not in m and app.posted[0][1] not in m


def test_foreign_or_forwarded_contact_is_refused_and_no_flow_is_ignored():
    import asyncio
    from bot.texts import t
    app, ctx = _phone_bot(), _ctx(["v_" + PHONE_TOKEN])
    upd, _ = _phone_update()
    asyncio.run(app.start(upd, ctx))
    for kw in ({"contact_user_id": 8}, {"contact_user_id": None}, {"forwarded": True}):
        upd, replies = _phone_update(**kw)
        asyncio.run(app.on_contact(upd, ctx))
        assert replies == [(t("ru", "PHONE_NOT_OWN"), None)] and app.posted == [], kw
    # Without a pending website link a contact is ignored completely.
    app2 = _phone_bot()
    upd, replies = _phone_update()
    asyncio.run(app2.on_contact(upd, _ctx()))
    assert replies == [] and app2.posted == []


def test_expired_pending_flow_and_result_texts_in_three_languages():
    import asyncio
    import time
    from bot.main import PHONE_PENDING_KEY
    from bot.texts import t
    app = _phone_bot()
    ctx = _ctx(user_data={PHONE_PENDING_KEY: (PHONE_TOKEN, time.monotonic() - 1)})
    upd, replies = _phone_update()
    asyncio.run(app.on_contact(upd, ctx))
    assert replies[0][0] == t("ru", "PHONE_EXPIRED") and app.posted == []
    keys = {"ok": "PHONE_OK", "phone_used": "PHONE_USED", "other_phone": "PHONE_OTHER", "invalid": "PHONE_EXPIRED",
            "unavailable": "PHONE_UNAVAILABLE"}
    for status, key in keys.items():
        for lang in ("ru", "kk", "en"):
            app = _phone_bot(status=status)
            ctx = _ctx(user_data={PHONE_PENDING_KEY: (PHONE_TOKEN, time.monotonic() + 60)})
            upd, replies = _phone_update(lang)
            asyncio.run(app.on_contact(upd, ctx))
            assert replies[0][0] == t(lang, key), (status, lang)
    assert "уже получал пробный период" in t("ru", "PHONE_USED") and "already received a trial" in t("en", "PHONE_USED")


def test_site_request_is_signed_and_carries_no_number():
    import asyncio
    import hashlib
    import hmac
    import json as _json
    import httpx
    from bot import phone_verify as pv
    from bot.main import post_phone_verified
    seen = []
    fp = pv.phone_fp(PHONE_SECRET, "12025550143")

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "phone_used"})

    status = asyncio.run(post_phone_verified("http://127.0.0.1:8000/", PHONE_SECRET, PHONE_TOKEN, fp,
                                             transport=httpx.MockTransport(handler), now=1_800_000_000))
    assert status == "phone_used"
    req = seen[0]
    assert req.method == "POST" and str(req.url) == "http://127.0.0.1:8000/internal/phone-verified"
    body = _json.loads(req.content)
    assert body == {"token": PHONE_TOKEN, "phone_fp": fp, "ts": 1_800_000_000}
    assert "555" not in req.content.decode()
    expected = hmac.new(PHONE_SECRET.encode(), f"{PHONE_TOKEN}|{fp}|1800000000".encode(), hashlib.sha256)
    assert req.headers[pv.SIGNATURE_HEADER] == expected.hexdigest()

    def answer(code, payload):
        return lambda request: httpx.Response(code, json=payload)

    def boom(request):
        raise httpx.ConnectError("refused")

    for h in (answer(403, {"status": "denied"}), answer(429, {"status": "limited"}), answer(500, ["x"]), boom,
              lambda request: httpx.Response(200, text="<html>")):
        assert asyncio.run(post_phone_verified("http://x", PHONE_SECRET, PHONE_TOKEN, fp,
                                               transport=httpx.MockTransport(h))) == "unavailable"


def test_bot_config_phone_settings(monkeypatch):
    from bot.config import load
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.delenv("BOT_OWNER_ID", raising=False)
    monkeypatch.delenv("PHONE_VERIFY_SECRET", raising=False)
    monkeypatch.delenv("WEB_INTERNAL_URL", raising=False)
    cfg = load()
    assert cfg.phone_secret == "" and cfg.web_internal_url == "http://127.0.0.1:8000"
    monkeypatch.setenv("PHONE_VERIFY_SECRET", "short")
    with pytest.raises(RuntimeError):
        load()
    monkeypatch.setenv("PHONE_VERIFY_SECRET", PHONE_SECRET)
    assert PHONE_SECRET not in repr(load())


def test_contact_handler_is_registered():
    from telegram.ext import MessageHandler
    from bot.config import BotConfig
    from bot import main as bm
    cfg = BotConfig(token="1:x", owner_id=None, owner_contact="", prices_text="", free_docs=1, daily_doc_limit=1,
                    max_files_per_batch=1, db_path=":memory:", model="m")
    original = bm.BotApp.__init__
    bm.BotApp.__init__ = lambda self, c: (setattr(self, "cfg", c), None)[1]
    try:
        app = bm.build_application(cfg)
    finally:
        bm.BotApp.__init__ = original
    callbacks = [h.callback.__name__ for group in app.handlers.values() for h in group if isinstance(h, MessageHandler)]
    assert "on_contact" in callbacks
