"""Daily copy of the bot database (balances of paying users, counters). Run by skaner-backup.timer.

    python deploy/backup_db.py /opt/skaner/data/bot.sqlite3

Copies are made with SQLite's online backup (safe while the bot is running) into backups/ next to
the database; the newest KEEP copies are kept.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

KEEP = 14


def backup(db_path: Path) -> Path:
    folder = db_path.parent / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    target = folder / f"{db_path.stem}-{date.today().isoformat()}.sqlite3"
    source = sqlite3.connect(db_path)
    copy = sqlite3.connect(target)
    with copy:
        source.backup(copy)
    copy.close()
    source.close()
    target.chmod(0o600)
    for old in sorted(folder.glob(f"{db_path.stem}-*.sqlite3"))[:-KEEP]:
        old.unlink()
    return target


if __name__ == "__main__":
    print(backup(Path(sys.argv[1])))
