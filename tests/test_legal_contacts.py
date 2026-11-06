"""Legal pages (privacy, terms, security) and the Contacts page (a message to the owner)."""

import pytest
from sqlalchemy import select

from test_app import csrf
from test_plans_admin import CODE_RE, Env
from web.config import LANGS
from web.models import PaymentRequest

MARKERS = {"ru": "Редакция от", "kk": "Редакция:", "en": "Version of"}


@pytest.fixture
def env():
    e = Env()
    e.login()
    return e


@pytest.mark.parametrize("path", ["/privacy", "/terms", "/security"])
def test_legal_pages_render_in_three_languages(path):
    e = Env()
    seen = set()
    for lang in LANGS:
        html = e.new_client(lang).get(path).text
        assert f'<html lang="{lang}">' in html
        assert "legal.css" in html
        assert "Эта страница ещё готовится" not in html and "Coming soon" not in html
        seen.add(html.split("<main>")[1][:600])
    assert len(seen) == 3  # three different texts, not one language three times


def test_privacy_states_what_is_true():
    html = Env().new_client("ru").get("/privacy").text
    for fact in ("90 дней", "со своим ключом", "не записывается в базу", "не сохраняются", "отпечаток", "Германии", "удалить аккаунт"):
        assert fact in html


def test_legal_pages_need_no_login_and_have_no_inline_scripts():
    c = Env().new_client("en")
    for path in ("/privacy", "/terms", "/security", "/contacts"):
        r = c.get(path)
        assert r.status_code == 200
        assert "<script>" not in r.text and "style=" not in r.text


def test_contacts_asks_anonymous_visitors_to_sign_in():
    html = Env().new_client("ru").get("/contacts").text
    assert "/login?next=/contacts" in html and "<textarea" not in html


def test_contacts_post_by_anonymous_redirects_to_login():
    e = Env()
    c = e.new_client("ru")
    r = c.post("/contacts", data={"contact": "+7 701 111 22 33", "message": "Привет, вопрос", "csrf_token": csrf(c, "/contacts")},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_contacts_message_is_stored_encrypted_and_sent_to_the_owner(env):
    c = env.client
    token = csrf(c, "/contacts")
    r = c.post("/contacts", data={"contact": "+7 701 000 11 22", "message": "Секретный-вопрос про тарифы", "csrf_token": token})
    assert r.status_code == 200 and "Сообщение отправлено" in r.text
    with env.db() as s:
        row = s.scalar(select(PaymentRequest))
        assert row.plan == "question" and row.data_enc
        assert b"701 000" not in row.data_enc and "Секретный".encode() not in row.data_enc
    text = env.notifier.sent[-1]
    assert "message from the site" in text and "+7 701 000 11 22" in text and "Секретный-вопрос" in text
    assert "@" not in text and CODE_RE.search(text)


@pytest.mark.parametrize("contact,message,code", [
    ("name@mail.ru", "Длинное сообщение тут", "e_email"),
    ("+7 701 000 11 22", "ok", "e_short"),
    ("1", "Достаточно длинное сообщение", "e_contact"),
    ("+7 701 000 11 22", "bad\x00text here", "e_text"),
])
def test_contacts_validation(env, contact, message, code):
    c = env.client
    r = c.post("/contacts", data={"contact": contact, "message": message, "csrf_token": csrf(c, "/contacts")})
    assert r.status_code == 400 and 'role="alert"' in r.text
    with env.db() as s:
        assert s.scalar(select(PaymentRequest)) is None


def test_contacts_needs_csrf(env):
    r = env.client.post("/contacts", data={"contact": "+7 701 000 11 22", "message": "Привет, вопрос", "csrf_token": "0" * 64})
    assert r.status_code == 403


def test_contacts_daily_limit(env):
    c = env.client
    for _ in range(3):
        assert c.post("/contacts", data={"contact": "+7 701 000 11 22", "message": "Привет, вопрос",
                                         "csrf_token": csrf(c, "/contacts")}).status_code == 200
    r = c.post("/contacts", data={"contact": "+7 701 000 11 22", "message": "Привет, вопрос", "csrf_token": csrf(c, "/contacts")})
    assert r.status_code == 429


def test_hostile_values_are_escaped_where_the_owner_reads_them(env):
    c = env.client
    r = c.post("/contacts", data={"contact": "<script>alert(1)</script>", "message": "<img src=x onerror=alert(2)> вопрос",
                                  "csrf_token": csrf(c, "/contacts")})
    assert r.status_code == 200 and "<script>alert(1)</script>" not in r.text  # never echoed back
    page = env.admin().get("/admin/requests").text
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_admin_sees_the_message_labelled_as_a_question(env):
    c = env.client
    c.post("/contacts", data={"contact": "+7 701 000 11 22", "message": "Вопрос для владельца", "csrf_token": csrf(c, "/contacts")})
    page = env.admin().get("/admin/requests").text
    assert "Вопрос для владельца" in page and ">Question<" in page


def test_contacts_form_in_every_language(env):
    for lang in LANGS:
        c = env.login(env.new_client(lang), f"{lang}@example.com")
        html = c.get("/contacts").text
        assert "<textarea" in html and 'name="csrf_token"' in html


# ---------- legal texts: structure, honesty, consent ----------

import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
SUBPROCESSORS = ("Anthropic", "Oracle Cloud", "Cloudflare", "Turnstile", "Resend", "Telegram")


def _main(html: str) -> str:
    return html.split("<main>")[1].split("</main>")[0]


@pytest.mark.parametrize("path", ["/privacy", "/terms", "/security"])
def test_legal_pages_have_the_same_sections_in_every_language(path):
    e = Env()
    counts = {lang: _main(e.new_client(lang).get(path).text).count("<h2") for lang in LANGS}
    assert len(set(counts.values())) == 1, counts
    if path != "/security":
        assert counts["ru"] >= 10


@pytest.mark.parametrize("path", ["/privacy", "/terms", "/security"])
def test_legal_pages_carry_the_version_date(path):
    e = Env()
    for lang, marker in MARKERS.items():
        main = _main(e.new_client(lang).get(path).text)
        assert marker in main and "2026" in main, (path, lang)


def test_privacy_names_every_subprocessor_in_every_language():
    e = Env()
    for lang in LANGS:
        main = _main(e.new_client(lang).get("/privacy").text)
        for name in SUBPROCESSORS:
            assert name in main, (lang, name)
        assert "AES-256-GCM" in main and "HMAC" in main and "90" in main and "14" in main


def test_terms_have_the_liability_cap_and_governing_law():
    e = Env()
    expect = {
        "ru": ("Ответственность", "за 3 месяца до события", "Применимое право", "законодательство Республики Казахстан"),
        "kk": ("Жауапкершілік", "3 ай ішінде", "қолданылатын құқық", "Қазақстан Республикасының заңнамасы"),
        "en": ("Liability", "in the 3 months before the event", "governing law", "the law of the Republic of Kazakhstan"),
    }
    for lang, phrases in expect.items():
        main = _main(e.new_client(lang).get("/terms").text)
        for phrase in phrases:
            assert phrase.lower() in main.lower(), (lang, phrase)
        assert "как есть" in main or "қалай бар, солай" in main or '"as is"' in main


# Words that would be a promise or a claim the service cannot back. Each may appear only inside an allowed negation.
FORBIDDEN = ("100%", "гарантир", "guarantee", "кепілдік", "нейросет", "искусственн", "artificial intelligence",
             "жасанды интеллект")
ALLOWED = ("не гарантируем", "do not guarantee", "кепілдік бермейміз")
AI_WORD = re.compile(r"(?<![A-Za-zА-Яа-яЁё])(AI|ИИ)(?![A-Za-zА-Яа-яЁё])")


def _texts():
    for path in (WEB / "templates").glob("*.html"):
        yield path.name, path.read_text(encoding="utf-8")
    for path in (WEB / "locales").glob("*.json"):
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            yield f"{path.name}:{key}", value


def test_no_promises_or_overclaims_anywhere():
    for where, text in _texts():
        low = text.lower()
        for phrase in ALLOWED:
            low = low.replace(phrase, " ")
        for word in FORBIDDEN:
            assert word not in low, (where, word)
        assert not AI_WORD.search(text), where


@pytest.mark.parametrize("path", ["/", "/login"])
def test_sign_in_forms_show_the_consent_line_with_links(path):
    e = Env()
    for lang in LANGS:
        html = e.new_client(lang).get(path).text
        assert 'class="fine consent"' in html and 'href="/terms"' in html and 'href="/privacy"' in html, (path, lang)
    ru = Env().new_client("ru").get(path).text
    assert "Продолжая, вы принимаете" in ru and "для работы или бизнеса" in ru


def test_forms_that_collect_data_show_the_consent_line(env):
    for path in ("/contacts", "/app/plans/request?plan=start"):
        html = env.client.get(path).text
        assert 'class="fine consent"' in html, path
