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
# Alembic adoption and then stamped onto the migration chain. Kept in sync with the
# additive migrations: 0002-era drift is healed for volumes adopted before 0003 existed;
# 0003 adds audit_log.status_code and commits.signature.
_LEGACY_COLUMNS = {
    "commits": {"extra_parents": "TEXT", "signature": "TEXT",
                "env_snapshot_id": "TEXT"},
    "trees": {"chunks": "JSON"},
    "audit_log": {"status_code": "INTEGER"},
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


def _create_missing_tables(sync_conn, tables: set[str]) -> None:
    """Creates any models.py tables a legacy volume doesn't have yet (idempotent).

    Adoption stamps the ENTIRE chain as applied, which means later revisions never run
    on that volume — so a true v1.1.x-era create_all database (no runs/events/webhooks/
    audit_log/webhook_deliveries) would otherwise be stamped to head and silently stay
    without the autonomous-loop tables. Creating missing tables from the metadata before
    stamping closes that gap additively; existing tables are never touched.
    """
    import sqlalchemy as sa

    from .models import Base

    missing = [t for t in Base.metadata.sorted_tables if t.name not in tables]
    if missing:
        Base.metadata.create_all(sync_conn, tables=missing)


def _ensure_schema_sync(sync_conn, cfg) -> None:
    """Runs on a worker thread inside conn.run_sync — no ambient event loop here."""
    import sqlalchemy as sa
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(cfg)
    tables = set(sa.inspect(sync_conn).get_table_names())

    # A data table without a recorded revision means "schema exists, chain unrecorded" —
    # either a true pre-Alembic create_all volume (no version table at all) or a volume
    # whose version rows were lost/truncated while the tables stayed (same signature).
    # Both must be healed + stamped to head, never replayed: replaying 0001 into existing
    # tables crashes startup with DuplicateTableError (found by the live heal test).
    needs_adoption = "commits" in tables and _unrecorded_chain(sync_conn, tables)

    if needs_adoption:
        # Legacy create_all-era database: create any post-create_all tables, heal column
        # drift, then mark the entire existing chain applied so only FUTURE revisions ever
        # execute on it.
        _create_missing_tables(sync_conn, tables)
        _heal_legacy_columns(sync_conn, tables)
        MigrationContext.configure(sync_conn).stamp(script, script.get_current_head())

    cfg.attributes["connection"] = sync_conn
    command.upgrade(cfg, "head")


def _unrecorded_chain(sync_conn, tables: set) -> bool:
    """True when the migration chain has no recorded revision for this database."""
    from alembic.runtime.migration import MigrationContext

    if "alembic_version" not in tables:
        return True
    return MigrationContext.configure(sync_conn).get_current_revision() is None


async def _apply_schema(target_engine: AsyncEngine) -> None:
    """Brings `target_engine`'s database to the latest migration.

    MUST be `engine.begin()`, not a plain `connect()` context: SQLAlchemy 2.0's
    commit-as-you-go means a plain connection rolls everything back at context exit,
    and Postgres (unlike the pysqlite driver, whose DDL auto-commits) honours that —
    the v1.1.6–v1.1.8 CI runs executed every migration statement faithfully and then
    threw the whole schema away, silently. See Probleme.md #70.
    """
    cfg = _alembic_config()
    async with target_engine.begin() as conn:
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
