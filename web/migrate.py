"""Alembic helpers used by the app (dev auto-upgrade, production schema check). CLI: `alembic upgrade head`."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["skip_logging_config"] = True  # keep the app's logging setup untouched
    return cfg


def upgrade_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def is_at_head(engine: Engine, url: str) -> bool:
    heads = set(ScriptDirectory.from_config(alembic_config(url)).get_heads())
    with engine.connect() as conn:
        return set(MigrationContext.configure(conn).get_current_heads()) == heads
