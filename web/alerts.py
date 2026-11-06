"""Alerts to the owner (Telegram, through the same notifier as payment requests) for problems a human must fix:
the recognition account is out of money or its key was refused, login mails cannot be sent, the site crashed.

Every alert has a fixed text: exception messages are read only to tell the cases apart and are never forwarded
(they could hold document text). One alert per kind per time window, so a broken service sends a message or two,
not hundreds.
"""

import logging
import threading
import time
from typing import Callable

log = logging.getLogger("skaner.web.alerts")

HOUR = 3600.0


class Alerter:
    def __init__(self, notifier, clock: Callable[[], float] = time.monotonic):
        self._notifier, self._clock = notifier, clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def notify(self, kind: str, text: str, every: float = HOUR) -> bool:
        """Sends `text` unless this kind was sent within `every` seconds. Never raises."""
        now = self._clock()
        with self._lock:
            last = self._last.get(kind)
            if last is not None and now - last < every:
                return False
            self._last[kind] = now
        try:
            self._notifier.send("Skaner: " + text)
        except Exception as exc:  # noqa: BLE001  (an alert problem must not break the request or the job)
            log.warning("owner alert not sent: %s", type(exc).__name__)
        return True

    def recognition_failed(self, exc: BaseException) -> None:
        """A page or file failed in the recognition service. Hostile or broken files (the user's problem) say nothing."""
        from .jobs import error_code

        if error_code(exc) in ("file", "format"):
            return
        name, msg = type(exc).__name__, str(exc).lower()
        if "credit balance" in msg or "billing" in msg:
            self.notify("billing", "the Anthropic balance has run out or is too low: recognition is not working. "
                        "Top it up: console.anthropic.com → Billing.", 6 * HOUR)
        elif "api error 401" in msg or "api error 403" in msg or name in ("AuthenticationError", "PermissionDeniedError"):
            self.notify("key", "the Anthropic key was rejected: recognition is not working. Check the key in /opt/skaner/web.env.",
                        6 * HOUR)
        elif error_code(exc) == "service":
            self.notify("service", f"the recognition service failed ({name}). If it repeats, check the Anthropic status.",
                        3 * HOUR)
        else:
            self.notify("bug", f"internal error while processing documents ({name}). Log: journalctl -u skaner-web.")

    def mail_failed(self, exc: BaseException) -> None:
        self.notify("mail", f"sign-in code emails are not being sent ({type(exc).__name__}). Check Resend: the key and "
                    "limits. Until fixed, nobody can sign in.")

    def site_error(self, exc: BaseException, route: str) -> None:
        self.notify("site", f"error on the site ({type(exc).__name__}, {route}). Log: journalctl -u skaner-web.")
