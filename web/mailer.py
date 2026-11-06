"""Outgoing mail. Never logs addresses or message bodies.

- ConsoleMailer: dev only, prints the code and a masked address to stdout.
- ResendMailer: Resend HTTP API (https://resend.com/docs/api-reference/emails/send-email).
  Selected when RESEND_API_KEY is set. Outside dev without a key the app refuses to start.
"""

import logging
import re
import uuid
from typing import Protocol

import httpx

from .config import Settings
from .security import mask_email

log = logging.getLogger(__name__)
TIMEOUT = 5.0


class MailError(Exception):
    """Sending failed. The message never contains the address or the body."""


class Mailer(Protocol):
    def send(self, to: str, subject: str, text: str) -> None: ...


def _check_headers(*values: str) -> None:
    if any("\r" in v or "\n" in v for v in values):
        raise MailError("line break in a header value")


class ConsoleMailer:
    def send(self, to: str, subject: str, text: str) -> None:
        _check_headers(to, subject)
        m = re.search(r"\b\d{6}\b", text)
        print(f"[dev mail] to {mask_email(to)} code {m.group(0) if m else '-'}", flush=True)


class ResendMailer:
    URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str, sender: str, transport: httpx.BaseTransport | None = None):
        if not api_key or not sender:
            raise RuntimeError("RESEND_API_KEY and WEB_MAIL_FROM must both be set")
        _check_headers(sender)
        self._key, self._sender = api_key, sender
        # No retries: a retried POST could deliver the same code twice. Redirects are not followed.
        self._client = httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=False)

    def __repr__(self) -> str:
        return "ResendMailer(<hidden>)"

    def send(self, to: str, subject: str, text: str) -> None:
        _check_headers(to, subject)
        try:
            r = self._client.post(
                self.URL,
                headers={"Authorization": f"Bearer {self._key}", "Idempotency-Key": str(uuid.uuid4())},
                json={"from": self._sender, "to": [to], "subject": subject, "text": text},
            )
        except httpx.HTTPError as exc:
            raise MailError(f"mail API unreachable ({type(exc).__name__})") from None
        if not 200 <= r.status_code < 300:
            raise MailError(f"mail API returned {r.status_code}")


def make_mailer(settings: Settings) -> Mailer:
    if settings.resend_api_key:
        return ResendMailer(settings.resend_api_key, settings.mail_from)
    if settings.dev:
        return ConsoleMailer()
    raise RuntimeError("No mail provider: set RESEND_API_KEY and WEB_MAIL_FROM (refusing to start in production)")
