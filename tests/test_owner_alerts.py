"""Owner alerts: fixed texts, one per kind per window, no document text or addresses inside."""

import pytest
from fastapi.testclient import TestClient

from tests.test_auth import EMAIL, SECRET, Clock, FailingMailer, FakeMailer, ask
from web.alerts import HOUR, Alerter
from web.config import Settings
from web.main import create_app
from web.mailer import MailError


class Notes:
    configured = True

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class Tick:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ExtractionError(Exception):
    pass


class UnsupportedFileError(Exception):
    pass


def test_one_alert_per_kind_per_window():
    n, tick = Notes(), Tick()
    a = Alerter(n, tick)
    assert a.notify("x", "one") and not a.notify("x", "two") and a.notify("y", "other")
    tick.t += HOUR + 1
    assert a.notify("x", "three")
    assert n.sent == ["Skaner: one", "Skaner: other", "Skaner: three"]


def test_a_broken_notifier_never_raises():
    class Boom:
        def send(self, text):
            raise RuntimeError("telegram down")
    assert Alerter(Boom()).notify("x", "t") is True


@pytest.mark.parametrize("exc, word", [
    (ExtractionError("Claude API error 400: Your credit balance is too low to access the Anthropic API"), "balance"),
    (ExtractionError("Claude API error 401: invalid x-api-key"), "key"),
    (ExtractionError("Cannot reach the Claude API: timeout"), "service failed"),
    (KeyError("secret-document-text"), "internal error"),
])
def test_recognition_failures_are_classified_with_fixed_texts(exc, word):
    n = Notes()
    Alerter(n, Tick()).recognition_failed(exc)
    assert len(n.sent) == 1 and word in n.sent[0]
    for leak in ("credit balance is too low", "invalid x-api-key", "secret-document-text"):
        assert leak not in n.sent[0]


def test_users_own_bad_files_do_not_alert():
    n = Notes()
    a = Alerter(n, Tick())
    a.recognition_failed(UnsupportedFileError("a.xyz"))
    assert n.sent == []


def site(mailer=None):
    notes = Notes()
    app = create_app(Settings(db_url="sqlite://", secret=SECRET, base_url="http://testserver", dev=True),
                     mailer=mailer or FakeMailer(), clock=Clock(), notifier=notes)
    return app, notes


def test_failing_login_mail_alerts_the_owner_without_the_address():
    app, notes = site(FailingMailer())
    client = TestClient(app)
    assert ask(client).status_code == 303
    assert len(notes.sent) == 1 and "sign-in code emails" in notes.sent[0]
    assert "example.com" not in notes.sent[0] and "MailError" in notes.sent[0]
    ask(client, "second@example.org")  # same hour: no second alert
    assert len(notes.sent) == 1


def test_crash_gives_a_plain_500_and_an_alert_with_the_route_not_the_url():
    app, notes = site()

    @app.get("/boom/{secret}")
    def boom(secret: str):
        raise ValueError("document text that must not travel")

    r = TestClient(app, raise_server_exceptions=False).get("/boom/token-123")
    assert r.status_code == 500 and r.text == "Internal Server Error"
    assert len(notes.sent) == 1
    assert "ValueError" in notes.sent[0] and "/boom/{secret}" in notes.sent[0]
    assert "token-123" not in notes.sent[0] and "document text" not in notes.sent[0]


def test_batch_with_empty_balance_alerts(tmp_path):
    from web.jobs import Context
    n = Notes()
    ctx = Context(None, None, None, None, Alerter(n, Tick()))
    ctx.alert_recognition(ExtractionError("Claude API error 400: Your credit balance is too low"))
    assert len(n.sent) == 1 and "Billing" in n.sent[0]
    Context(None, None, None, None).alert_recognition(ExtractionError("x"))  # no alerter: silent
