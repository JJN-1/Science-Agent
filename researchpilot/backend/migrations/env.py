from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection, create_engine

from app.store.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_engine():
    engine = config.attributes.get("engine")
    if engine is not None:
        return engine
    url = context.get_x_argument(as_dictionary=True).get("db_url")
    if not url:
        raise RuntimeError("alembic 需要 engine（运行时注入）或 -x db_url=<sqlite url>")
    return create_engine(url, poolclass=pool.NullPool)


def run_migrations_offline() -> None:
    context.configure(
        literal_binds=True,
        dialect_name="sqlite",
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = _get_engine()
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
