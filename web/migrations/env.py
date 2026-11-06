"""Alembic environment. Database URL: sqlalchemy.url set by web.migrate, otherwise WEB_DB_URL (.env)."""

from logging.config import fileConfig

from alembic import context

from web.config import default_db_url
from web.db import Base, make_engine
from web import models  # noqa: F401  (registers tables)

config = context.config
if config.config_file_name and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
URL = config.get_main_option("sqlalchemy.url") or default_db_url()


def run_migrations_offline() -> None:
    context.configure(url=URL, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=URL.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(URL)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata,
                              render_as_batch=URL.startswith("sqlite"))
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
