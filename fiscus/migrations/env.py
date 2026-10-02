"""Alembic environment. Migrations run at start-up, before the app serves."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from fiscus import models  # noqa: F401  (registers every table on Base.metadata)
from fiscus.db import Base

config = context.config
target_metadata = Base.metadata


def run_offline() -> None:
    context.configure(
        url=config.get_main_option('sqlalchemy.url'),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
