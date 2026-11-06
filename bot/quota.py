"""Per-user document allowance and a global daily cap, stored in SQLite.

Only Telegram user ids and counters are stored, never documents or their contents.
"""

import sqlite3
import threading
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    used    INTEGER NOT NULL DEFAULT 0,
    credits INTEGER NOT NULL DEFAULT 0,
    created TEXT    NOT NULL DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS daily (
    day  TEXT PRIMARY KEY,
    docs INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    user_id INTEGER NOT NULL,
    name    TEXT    NOT NULL,
    first   TEXT    NOT NULL DEFAULT (date('now')),
    count   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, name)
);
CREATE TABLE IF NOT EXISTS user_daily (
    user_id       INTEGER NOT NULL,
    day           TEXT    NOT NULL,
    free_failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS feedback (
    kind    TEXT    NOT NULL,
    verdict TEXT    NOT NULL,
    count   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (kind, verdict)
);
"""

# Demand signals shown in /stats: who reached the paywall and who looked at the prices.
EVENTS = ("quota_exhausted", "plans_viewed")


class QuotaStore:
    def __init__(self, path: Path | str, free_docs: int, daily_limit: int):
        self.free_docs = free_docs
        self.daily_limit = daily_limit
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        try:  # databases created before the free-scan limit
            self._db.execute("ALTER TABLE user_daily ADD COLUMN scan_pages INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self._lock = threading.Lock()

    def _ensure_user(self, user_id: int) -> None:
        self._db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))

    def remaining(self, user_id: int) -> int:
        with self._lock:
            self._ensure_user(user_id)
            used, credits = self._db.execute(
                "SELECT used, credits FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            self._db.commit()
        return max(0, self.free_docs + credits - used)

    def daily_remaining(self, today: date | None = None) -> int:
        day = (today or date.today()).isoformat()
        with self._lock:
            row = self._db.execute("SELECT docs FROM daily WHERE day = ?", (day,)).fetchone()
        return max(0, self.daily_limit - (row[0] if row else 0))

    def consume(self, user_id: int, docs: int, today: date | None = None) -> None:
        day = (today or date.today()).isoformat()
        with self._lock:
            self._ensure_user(user_id)
            self._db.execute("UPDATE users SET used = used + ? WHERE user_id = ?", (docs, user_id))
            self._db.execute(
                "INSERT INTO daily (day, docs) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET docs = docs + excluded.docs",
                (day, docs),
            )
            self._db.commit()

    def add_daily(self, pages: int, today: date | None = None) -> None:
        """Count work that was not billed (technical errors) toward the global daily cap: it still costs money."""
        if pages <= 0:
            return
        day = (today or date.today()).isoformat()
        with self._lock:
            self._db.execute(
                "INSERT INTO daily (day, docs) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET docs = docs + excluded.docs",
                (day, pages),
            )
            self._db.commit()

    def free_failures_used(self, user_id: int, today: date | None = None) -> int:
        day = (today or date.today()).isoformat()
        with self._lock:
            row = self._db.execute(
                "SELECT free_failures FROM user_daily WHERE user_id = ? AND day = ?", (user_id, day)
            ).fetchone()
        return row[0] if row else 0

    def add_free_failures(self, user_id: int, count: int, today: date | None = None) -> None:
        if count <= 0:
            return
        day = (today or date.today()).isoformat()
        with self._lock:
            self._db.execute(
                "INSERT INTO user_daily (user_id, day, free_failures) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, day) DO UPDATE SET free_failures = free_failures + excluded.free_failures",
                (user_id, day, count),
            )
            self._db.commit()

    def scan_pages_used(self, user_id: int, today: date | None = None) -> int:
        day = (today or date.today()).isoformat()
        with self._lock:
            row = self._db.execute(
                "SELECT scan_pages FROM user_daily WHERE user_id = ? AND day = ?", (user_id, day)
            ).fetchone()
        return row[0] if row else 0

    def add_scan_pages(self, user_id: int, pages: int, today: date | None = None) -> None:
        day = (today or date.today()).isoformat()
        with self._lock:
            self._db.execute(
                "INSERT INTO user_daily (user_id, day, scan_pages) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, day) DO UPDATE SET scan_pages = scan_pages + excluded.scan_pages",
                (user_id, day, pages),
            )
            self._db.commit()

    def grant(self, user_id: int, docs: int) -> int:
        """Add paid documents to a user. Returns the new remaining allowance."""
        with self._lock:
            self._ensure_user(user_id)
            self._db.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (docs, user_id))
            self._db.commit()
        return self.remaining(user_id)

    def event(self, user_id: int, name: str) -> None:
        """Remember that a user did something that signals demand (one row per user and event)."""
        with self._lock:
            self._db.execute(
                "INSERT INTO events (user_id, name) VALUES (?, ?) "
                "ON CONFLICT(user_id, name) DO UPDATE SET count = count + 1",
                (user_id, name),
            )
            self._db.commit()

    def feedback(self, kind: str, verdict: str) -> None:
        """Count a 👍/👎 on a result. Anonymous: no user id, no text."""
        with self._lock:
            self._db.execute(
                "INSERT INTO feedback (kind, verdict) VALUES (?, ?) "
                "ON CONFLICT(kind, verdict) DO UPDATE SET count = count + 1",
                (kind, verdict),
            )
            self._db.commit()

    def stats(self, today: date | None = None) -> dict:
        day = (today or date.today()).isoformat()
        with self._lock:
            users, used = self._db.execute("SELECT COUNT(*), COALESCE(SUM(used), 0) FROM users").fetchone()
            row = self._db.execute("SELECT docs FROM daily WHERE day = ?", (day,)).fetchone()
            new_today = self._db.execute("SELECT COUNT(*) FROM users WHERE created = ?", (day,)).fetchone()[0]
            paid = self._db.execute("SELECT COUNT(*) FROM users WHERE credits > 0").fetchone()[0]
            votes = dict(self._db.execute(
                "SELECT verdict, SUM(count) FROM feedback GROUP BY verdict"
            ).fetchall())
            events = dict(self._db.execute(
                "SELECT name, COUNT(*) FROM events GROUP BY name"
            ).fetchall())
        return {
            "users": users, "new_today": new_today, "docs_total": used, "docs_today": row[0] if row else 0,
            "exhausted": events.get("quota_exhausted", 0), "plans_viewed": events.get("plans_viewed", 0),
            "paid_users": paid, "fb_good": votes.get("good", 0), "fb_bad": votes.get("bad", 0),
        }
