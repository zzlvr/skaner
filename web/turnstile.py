"""Bot protection for the login form: Cloudflare Turnstile (https://developers.cloudflare.com/turnstile/).

The form carries a widget; the browser gets a one-time token; `Turnstile.check` asks Cloudflare whether it is valid.
Fails closed: any error, timeout or unexpected answer counts as "not verified". The token and the secret are never logged.
"""

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)
URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT = 5.0
MAX_TOKEN = 2048  # Cloudflare's documented maximum


class Turnstile:
    def __init__(self, secret: str, transport: httpx.BaseTransport | None = None):
        if not secret:
            raise RuntimeError("TURNSTILE_SECRET is empty")
        self._secret = secret
        self._client = httpx.Client(timeout=TIMEOUT, transport=transport, follow_redirects=False)

    def __repr__(self) -> str:
        return "Turnstile(<hidden>)"

    def check(self, token: str, ip: str | None) -> bool:
        if not token or len(token) > MAX_TOKEN:
            return False
        data = {"secret": self._secret, "response": token}
        if ip:
            data["remoteip"] = ip
        try:
            r = self._client.post(URL, data=data)
            return r.status_code == 200 and r.json().get("success") is True
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.warning("captcha check failed: %s", type(exc).__name__)
            return False


def make_turnstile(settings: Settings) -> Turnstile | None:
    return Turnstile(settings.turnstile_secret) if settings.turnstile_secret and settings.turnstile_site_key else None
