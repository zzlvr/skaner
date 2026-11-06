"""Login bot check (Cloudflare Turnstile) and the site-wide cap on login mails."""

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_auth import EMAIL, SECRET, Clock, FakeMailer, ask, token
from web.config import Settings
from web.main import create_app
from web.turnstile import Turnstile, make_turnstile


class Recorder:
    def __init__(self):
        self.sent = []
        self.configured = True

    def send(self, text):
        self.sent.append(text)
        return True


def transport(success=True, status=200, body=None, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(status, content=body if body is not None else json.dumps({"success": success}))
    return httpx.MockTransport(handler)


def site(turnstile=None, **cfg):
    clock, mailer, notifier = Clock(), FakeMailer(), Recorder()
    settings = Settings(db_url="sqlite://", secret=SECRET, base_url="http://testserver", dev=True,
                        turnstile_site_key="site-key" if turnstile else "", **cfg)
    app = create_app(settings, mailer=mailer, clock=clock, notifier=notifier, turnstile=turnstile)
    return app, TestClient(app, headers={"accept-language": "ru"}), clock, mailer, notifier


# ---------- Turnstile ----------

def test_widget_and_csp_only_with_turnstile():
    _, plain, *_ = site()
    page = plain.get("/login")
    assert "cf-turnstile" not in page.text and "challenges.cloudflare.com" not in page.headers["content-security-policy"]
    _, guarded, *_ = site(Turnstile("secret", transport()))
    page = guarded.get("/login")
    assert 'class="cf-turnstile"' in page.text and 'data-sitekey="site-key"' in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self' https://challenges.cloudflare.com" in csp and "frame-src https://challenges.cloudflare.com" in csp
    # every other page keeps the strict policy
    assert "challenges.cloudflare.com" not in guarded.get("/").headers["content-security-policy"]


def test_valid_token_sends_the_code_and_secret_goes_to_cloudflare():
    calls = []
    _, client, _, mailer, _ = site(Turnstile("the-secret", transport(calls=calls)))
    r = ask(client, **{"cf-turnstile-response": "good-token"})
    assert r.status_code == 303 and len(mailer.sent) == 1
    assert calls[0]["secret"] == "the-secret" and calls[0]["response"] == "good-token"


@pytest.mark.parametrize("extra", [{}, {"cf-turnstile-response": ""}, {"cf-turnstile-response": "x" * 3000}])
def test_missing_or_oversized_token_is_refused(extra):
    _, client, _, mailer, _ = site(Turnstile("s", transport()))
    r = ask(client, **extra)
    assert r.status_code == 400 and not mailer.sent


@pytest.mark.parametrize("t", [
    transport(success=False), transport(status=500), transport(body="not json"), transport(body='"success"'),
    transport(body='{"success": "true"}'),
])
def test_cloudflare_says_no_or_misbehaves_fails_closed(t):
    _, client, _, mailer, _ = site(Turnstile("s", t))
    r = ask(client, **{"cf-turnstile-response": "tok"})
    assert r.status_code == 400 and not mailer.sent


def test_cloudflare_unreachable_fails_closed():
    def boom(request):
        raise httpx.ConnectError("down")
    _, client, _, mailer, _ = site(Turnstile("s", httpx.MockTransport(boom)))
    assert ask(client, **{"cf-turnstile-response": "tok"}).status_code == 400 and not mailer.sent


def test_failed_check_page_is_neutral_about_the_address():
    _, client, _, _, _ = site(Turnstile("s", transport(success=False)))
    r = ask(client, **{"cf-turnstile-response": "tok"})
    assert "Не удалось проверить" in r.text and "cf-turnstile" in r.text


def test_config_needs_both_keys(monkeypatch):
    monkeypatch.setenv("WEB_DEV", "1")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "only-site")
    monkeypatch.delenv("TURNSTILE_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        Settings.load()
    monkeypatch.setenv("TURNSTILE_SECRET", "both")
    assert Settings.load().turnstile_site_key == "only-site"


def test_make_turnstile_off_without_keys():
    assert make_turnstile(Settings(db_url="sqlite://", secret=SECRET, base_url="http://x", dev=True)) is None
    assert "both" not in repr(Turnstile("both"))


# ---------- site-wide mail cap ----------

def addresses(client, n, start=0):
    return [ask(client, f"user{start + i}@example.org") for i in range(n)]


def test_hourly_cap_stops_new_codes_and_alerts_the_owner_once():
    app, client, clock, mailer, notifier = site(mail_hourly=3, mail_daily=100)
    # one client IP is limited to 20 requests an hour: enough for this test
    rs = addresses(client, 3)
    assert all(r.status_code == 303 for r in rs) and len(mailer.sent) == 3
    r = ask(client, "another@example.org")
    assert r.status_code == 503 and "слишком много писем" in r.text.lower() and len(mailer.sent) == 3
    assert ask(client, "yet.another@example.org").status_code == 503
    assert len(notifier.sent) == 1 and "лимит писем" in notifier.sent[0]
    # the answer does not depend on the address: a known one gets it too
    assert ask(client, "user0@example.org").status_code == 503
    # the hour passes: codes flow again
    clock.advance(hours=1, minutes=1)
    assert ask(client, "later@example.org").status_code == 303 and len(mailer.sent) == 4


def test_daily_cap_and_next_day_alert():
    app, client, clock, mailer, notifier = site(mail_hourly=100, mail_daily=2)
    addresses(client, 2)
    assert ask(client, "a@example.org").status_code == 503 and len(notifier.sent) == 1
    clock.advance(hours=25)
    assert ask(client, "b@example.org").status_code == 303
    addresses(client, 1, start=50)
    assert ask(client, "c@example.org").status_code == 503 and len(notifier.sent) == 2


def test_cap_message_in_every_language():
    from pathlib import Path
    for lang in ("ru", "en", "kk"):
        data = json.loads((Path(__file__).parent.parent / "web" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
        assert data["mail_busy"] and data["captcha_failed"]
