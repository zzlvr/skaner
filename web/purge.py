"""Daily retention run: `python -m web.purge` (called by skaner-backup.service every night).

Deletes results past their 90 days, expired scan files and payment requests, old invitations, and old login codes and
counters. The site runs the same cleanup at start-up and on cabinet visits, but a quiet site must not keep data past
the periods promised in the privacy policy, so it also runs on a timer. Prints counts only.
"""

from . import auth
from .cabinet import purge_results
from .config import Settings
from .db import make_engine, make_sessionmaker
from .main import utcnow


def main() -> int:
    settings = Settings.load()
    engine = make_engine(settings.db_url)
    try:
        with make_sessionmaker(engine)() as db:
            now = utcnow()
            batches = purge_results(db, now)
            auth.purge_expired(db, now)
            guests = auth.purge_guests(db, now)
            db.commit()
        print(f"retention run: {batches} expired batches and {guests} finished guest sessions deleted")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
