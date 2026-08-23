"""Alembic migrations for the av_server schema.

Runs programmatically at server startup (see `av_server/database.py::init_db`) via
`alembic.command.upgrade` with a live sync connection passed through
`config.attributes["connection"]` — that avoids a nested event loop inside uvicorn's
async lifespan. When invoked manually (`alembic -x ... upgrade head`, no connection
attribute), env.py builds its own async engine from DATABASE_URL / sqlalchemy.url.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

import os  # noqa: E402

from av_server.models import Base  # noqa: E402

target_metadata = Base.metadata


def _database_url() -> str:
    return (
        config.get_main_option("sqlalchemy.url")
        or os.environ.get("DATABASE_URL")
        or "postgresql+asyncpg://av_user:av_password@db:5432/aether_vault"
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DBAPI connection (--sql mode)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    """Shared body: configure context on an already-open (sync-proxied) connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Manual CLI runs without a shared connection: build our own async engine."""
    connectable = async_engine_from_config(
        {"sqlalchemy.url": _database_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
    await connectable.dispose()


shared_connection = config.attributes.get("connection")
if shared_connection is not None:
    # Programmatic startup path (see module docstring): migrations execute directly on
    # the caller's sync-proxied connection — no nested event loop anywhere.
    _run_sync_migrations(shared_connection)
elif context.is_offline_mode():
    run_migrations_offline()
else:
    # Plain CLI/script invocation: no ambient loop here to conflict with.
    asyncio.run(run_migrations_online())
