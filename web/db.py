"""Database: SQLAlchemy (SQLite now, PostgreSQL later by changing WEB_DB_URL)."""

from pathlib import Path

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

# Stable constraint names: needed for portable Alembic migrations (SQLite batch mode, PostgreSQL).
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def is_memory_sqlite(url: str) -> bool:
    return url in ("sqlite://", "sqlite:///:memory:") or (url.startswith("sqlite") and "mode=memory" in url)


def make_engine(url: str) -> Engine:
    if is_memory_sqlite(url):
        # One shared connection, so every request (any thread) sees the same in-memory database. Tests only.
        engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, pool_pre_ping=not url.startswith("sqlite"))
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(conn, _):  # WAL: readers don't block the writer; foreign keys on
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)


def ensure_schema(engine: Engine, url: str, dev: bool) -> None:
    """In-memory (tests): create tables. Dev: apply migrations. Production: refuse to start on an old schema."""
    from . import models  # noqa: F401  (registers tables on Base.metadata)

    if is_memory_sqlite(url):
        Base.metadata.create_all(engine)
        return
    from .migrate import is_at_head, upgrade_head

    if dev:
        upgrade_head(url)
    elif not is_at_head(engine, url):
        raise RuntimeError("Database schema is not up to date: run `alembic upgrade head` before starting")
