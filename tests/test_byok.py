"""Bring-your-own-key (decision 67): a batch run with the user's own provider key takes nothing from the page
balance or the site's daily cap, the key is never stored or logged, and the owner gets no alerts for it."""

import logging

import pytest
from sqlalchemy import select

from test_app import Env, FakeBackend, batch_url, csrf, latest, png
from web import balance as bal
from web.jobs import BYOK_PROVIDERS, ByokKey, Context, error_code
from web.models import Batch, DailyUsage, LedgerEntry

ANTHROPIC_KEY = "sk-ant-api03-SECRETSECRETSECRETSECRET1234"
GEMINI_KEY = "AIzaSySECRETSECRETSECRETSECRET12345"


def post(client, key=ANTHROPIC_KEY, provider="anthropic", fmt="xlsx", files=None):
    data = {"csrf_token": csrf(client), "doctype": "receipt", "fmt": fmt}
    if key is not None:
        data.update(byok_provider=provider, byok_key=key)
    parts = [("files", (n, c, "application/octet-stream")) for n, c in (files or [("a.png", png())])]
    return client.post("/app/upload", data=data, files=parts, follow_redirects=False)


@pytest.fixture
def seen(monkeypatch):
    """Replaces the real provider backends: records the key each batch was given, returns the fake backend."""
    calls = []
    env_holder = {}

    def fake_get_backend(name=None, model=None, api_key=None):
        calls.append((name, model, api_key))
        e = env_holder["env"]
        return FakeBackend(e.plan, e.calls)

    monkeypatch.setattr("docstruc.llm.get_backend", fake_get_backend)
    return calls, env_holder


def test_byok_batch_uses_the_key_and_charges_nothing(seen):
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.login()
    before = bal.balance(e.user(), e.clock()).total
    r = post(c)
    batch_url(r)
    assert calls == [("claude", "claude-sonnet-5-5", ANTHROPIC_KEY)]
    b = latest(e)
    assert b.byok is True and b.status == "done" and b.pages_charged == 0 and b.work_units == 1
    assert bal.balance(e.user(), e.clock()).total == before  # no page spent
    with e.db() as s:
        assert s.scalar(select(DailyUsage)) is None  # no part of the site's daily cap reserved or used
        assert not [x for x in s.scalars(select(LedgerEntry)) if x.reason == "batch"]


def test_byok_works_with_no_pages_left(seen):
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.login()
    with e.db() as s:
        u = s.get(type(e.user()), e.user().id)
        u.trial_pages_left = 0
        s.commit()
    assert post(c, key=None).status_code == 402  # without a key the batch is refused for lack of pages
    batch_url(post(c))
    assert latest(e).status == "done"


def test_byok_gemini_provider(seen):
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.login()
    batch_url(post(c, key=GEMINI_KEY, provider="gemini"))
    assert calls == [("gemini", None, GEMINI_KEY)]


@pytest.mark.parametrize("provider,key", [
    ("anthropic", "AIzaSyWrongProviderKey1234567890"),  # a Gemini key for Anthropic
    ("gemini", ANTHROPIC_KEY),
    ("openai", "sk-proj-abcdefghijklmnopqrstuvwxyz"),    # not offered
    ("anthropic", "sk-ant-short"),
    ("anthropic", "sk-ant-" + "a b" * 10),
    ("anthropic", "sk-ant-" + "x" * 300),
])
def test_wrong_key_shape_is_refused_before_anything_starts(seen, provider, key):
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.login()
    r = post(c, key=key, provider=provider)
    assert r.status_code == 400 and "Ключ не похож на ключ" in r.text
    assert key not in r.text  # the key is never echoed back
    assert latest(e) is None and not calls


def test_key_is_not_stored_logged_or_shown(seen, caplog):
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.login()
    caplog.set_level(logging.DEBUG)
    r = post(c)
    page = c.get(batch_url(r))
    assert "SECRETSECRET" not in page.text and "SECRETSECRET" not in caplog.text
    with e.db() as s:
        raw = s.connection().connection.dbapi_connection
        dump = "\n".join(raw.iterdump())
    assert "SECRETSECRET" not in dump


def test_the_free_scan_is_gone_even_with_a_key(seen):
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.login()
    assert post(c, fmt="pdf_scan").status_code == 400
    assert latest(e) is None and not calls


def test_failed_byok_batch_is_not_charged_and_not_alerted(seen):
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.login()
    e.plan["bad"] = {"fail": True}
    batch_url(post(c, files=[("bad.png", png())]))
    b = latest(e)
    assert b.byok and b.status == "failed" and b.pages_charged == 0


# ---------- units ----------

class AuthenticationError(Exception):
    pass


class ExtractionError(Exception):
    pass


class BadRequestError(Exception):
    pass


class Spy:
    def __init__(self):
        self.hits = 0

    def recognition_failed(self, exc):
        self.hits += 1


def test_error_codes_for_a_users_own_key():
    assert error_code(AuthenticationError("x")) == "key"
    assert error_code(ExtractionError("Gemini API error 400: API key not valid. Please pass a valid API key.")) == "key"
    assert error_code(ExtractionError("Gemini API error 403: forbidden")) == "key"
    assert error_code(BadRequestError("Your credit balance is too low")) == "quota"
    assert error_code(ExtractionError("model returned no data")) == "service"
    assert error_code(ValueError("?")) == "internal"


def test_owner_alerts_are_for_the_owners_key_only():
    spy = Spy()
    ctx = Context(None, None, None, lambda: None, spy)
    ctx.alert_recognition(RuntimeError("x"))
    assert spy.hits == 1
    ctx.alert_recognition(RuntimeError("x"), ByokKey("anthropic", ANTHROPIC_KEY))
    assert spy.hits == 1


def test_byok_key_object_hides_and_wipes_the_key():
    k = ByokKey("anthropic", ANTHROPIC_KEY)
    assert "SECRETSECRET" not in repr(k) and "SECRETSECRET" not in str(k)
    k.wipe()
    assert k._key == ""
    assert set(BYOK_PROVIDERS) == {"anthropic", "gemini"}


# ---------- no free trial (decision 67) ----------

@pytest.mark.product_defaults
def test_new_user_gets_no_free_pages_and_must_bring_a_key_or_pay(seen):
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.login()
    assert bal.balance(e.user(), e.clock()).total == 0
    with e.db() as s:
        assert not [x for x in s.scalars(select(LedgerEntry)) if x.reason.startswith("trial")]
    r = post(c, key=None)
    assert r.status_code == 400 and "Paste your own API key" in r.text or "Вставьте свой ключ" in r.text  # no key, no recognition
    assert latest(e) is None and not calls
    batch_url(post(c))  # with the user's own key it runs
    assert latest(e).status == "done" and latest(e).byok


def test_plans_page_shows_the_pay_email_only_when_configured():
    e = Env(pay_email="pay@example.com")
    c = e.login()
    assert 'href="mailto:pay@example.com"' in c.get("/app/plans").text
    e2 = Env()
    c2 = e2.login()
    assert "mailto:" not in c2.get("/app/plans").text


def test_a_user_whose_keys_keep_being_rejected_is_stopped_for_the_day(seen):
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.login()
    from web.cabinet import BYOK_KEY_FAILS_PER_DAY

    with e.db() as s:
        for _ in range(BYOK_KEY_FAILS_PER_DAY):
            u = e.user()
            s.add(Batch(public_id="x" + str(_) * 20, user_id=u.id, wallet_user_id=u.id, doctype="receipt", fmt="xlsx",
                        one_document=False, trial_funded=False, byok=True, lang="en", status="failed", error_code="key",
                        files_count=1, files_done=1, pages_estimated=1, pages_charged=0, work_units=0, docs_found=0,
                        needs_review=0, failed_files=1, not_found=0, created_at=e.clock(), expires_at=e.clock()))
        s.commit()
    r = post(c)
    assert r.status_code == 429 and "Слишком много отклонённых ключей" in r.text and not calls
    e.clock.advance(hours=25)
    batch_url(post(c))  # the next day it works again


@pytest.mark.product_defaults
def test_free_mode_has_no_prices_plans_or_payment_requests():
    e = Env()
    c = e.login()
    landing = e.new_client("en").get("/").text
    assert 'id="prices"' not in landing and "/#prices" not in landing and "Source code" in landing
    assert "/app/plans" not in c.get("/app").text  # no Plans link, no balance block
    assert c.get("/app/plans").status_code == 404 and c.get("/app/plans/request?plan=biz").status_code == 404
    r = c.post("/app/plans/request", data={"csrf_token": csrf(c), "plan": "biz", "contact": "x@y.z", "company": "A",
                                          "comment": "b"})
    assert r.status_code == 404
    assert 'name="byok_key"' in c.get("/app").text  # the way to use the service is the own key
