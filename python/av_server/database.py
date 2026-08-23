import os
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Default points to the Docker service name; override via DATABASE_URL env var.
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://av_user:av_password@db:5432/aether_vault",
)

engine: AsyncEngine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

async_session_factory = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Columns introduced after the create_all era began. Legacy databases (created by the
# pre-Alembic startup) may lack them; they are healed zero-touch at first boot with the
# Alembic adoption and then stamped onto the migration chain.
_LEGACY_COLUMNS = {
    "commits": {"extra_parents": "TEXT"},
    "trees": {"chunks": "JSON"},
}


def _alembic_config():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def _heal_legacy_columns(sync_conn, tables: set[str]) -> None:
    """Adds any post-create_all-era columns a legacy database is missing (idempotent)."""
    import sqlalchemy as sa

    inspector = sa.inspect(sync_conn)
    for table, cols in _LEGACY_COLUMNS.items():
        if table not in tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for col, ddl in cols.items():
            if col not in present:
                sync_conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def _ensure_schema_sync(sync_conn, cfg) -> None:
    """Runs on a worker thread inside conn.run_sync — no ambient event loop here."""
    import sqlalchemy as sa
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    tables = set(sa.inspect(sync_conn).get_table_names())

    if "commits" in tables and "alembic_version" not in tables:
        # Legacy create_all-era database: heal post-adoption column drift, then mark the
        # entire existing chain applied so only FUTURE revisions ever execute on it.
        _heal_legacy_columns(sync_conn, tables)
        MigrationContext.configure(sync_conn).stamp(script, script.get_current_head())

    # Migrations execute through env.py on this same connection (passed via attributes),
    # keeping everything inside the caller's transaction/connection semantics.
    cfg.attributes["connection"] = sync_conn
    command.upgrade(cfg, "head")


async def _apply_schema(target_engine: AsyncEngine) -> None:
    """Brings `target_engine`'s database to the latest migration."""
    cfg = _alembic_config()
    async with target_engine.connect() as conn:
        await conn.run_sync(_ensure_schema_sync, cfg)


async def init_db() -> None:
    """Replaces the old create_all startup: schema is owned by Alembic from now on.

    Fresh databases walk the full migration chain; legacy databases (pre-Alembic
    create_all volumes) are detected by 'commits exists but no alembic_version',
    healed of known column drift, stamped to head, and confirmed by the resulting
    no-op upgrade. Failures fail startup loudly — a silently wrong schema would be
    worse than a down server.
    """
    await _apply_schema(engine)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a single DB session per request."""
    async with async_session_factory() as session:
        yield session
