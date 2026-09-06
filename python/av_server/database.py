import os
from pathlib import Path
from typing import AsyncGenerator

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

# Default points to the Docker service name; override via DATABASE_URL env var.
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://av_user:av_password@db:5432/aether_vault",
)

engine: AsyncEngine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

# `engine`/`DATABASE_URL` above (today's `av_user`) MUST keep having DDL rights --
# Alembic and `system_session_factory` both still use it unconditionally.
# `AV_APP_DATABASE_URL` is a SEPARATE, optional connection string for ordinary
# request-serving sessions, meant to point at the `av_app` role (SELECT/INSERT/UPDATE/
# DELETE only, no DDL/SUPERUSER/BYPASSRLS -- not exempt from row-level security like
# `av_user` is). Unset means `async_session_factory` below keeps using the same `engine`.
APP_DATABASE_URL: str | None = os.environ.get("AV_APP_DATABASE_URL") or None
app_engine: AsyncEngine = (
    create_async_engine(APP_DATABASE_URL, echo=False, pool_pre_ping=True)
    if APP_DATABASE_URL
    else engine
)

# Kept in sync with models.py::DEFAULT_TENANT_ID by a same-string-literal contract
# (importing models here would be circular).
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# Whether row-level security is actually ENFORCED (the GUC below is set at all). Off by
# default so an unconfigured deployment behaves byte-identically to pre-v1.3.2.
# `tenant_id` is still ALWAYS populated on every insert regardless of this flag (the
# column is NOT NULL unconditionally); only the GUC-setting/filtering is gated.
TENANCY_ENFORCE: bool = os.environ.get("AV_TENANCY_ENFORCE", "0") == "1"


class TenantScopedSession(Session):
    """A dedicated Session subclass (not the bare `Session` class) so the event
    listeners below are scoped to av_server's own session factories only — never firing
    for some other SQLAlchemy Session that might exist in-process (a test harness's
    own, for instance)."""


@event.listens_for(TenantScopedSession, "before_flush")
def _populate_tenant_id(session, flush_context, instances) -> None:
    """Every newly-inserted row on a tenant-scoped model gets a tenant_id, UNCONDITIONALLY
    -- not gated by TENANCY_ENFORCE, because the column is NOT NULL at the schema level
    regardless. Falls back to DEFAULT_TENANT_ID, matching how every pre-existing row was
    backfilled. This keeps every `db.add(DBXxx(...))` call site across server.py
    unchanged -- none of them needs to know about tenancy at all."""
    tenant_id = session.info.get("tenant_id") or DEFAULT_TENANT_ID
    for obj in session.new:
        if hasattr(obj, "tenant_id") and getattr(obj, "tenant_id", None) is None:
            obj.tenant_id = tenant_id


# A fixed, arbitrary lock key -- any stable int works. Scopes `pg_advisory_xact_lock` to
# exactly this one purpose so it never contends with an unrelated advisory lock elsewhere.
_AUDIT_CHAIN_LOCK_KEY = 881736452901

# A SEPARATE fixed key for serializing schema migrations across N replicas booting
# concurrently against the same fresh database -- see `_apply_schema`'s docstring.
_SCHEMA_MIGRATION_LOCK_KEY = 881736452902


@event.listens_for(TenantScopedSession, "before_flush")
def _chain_audit_log(session, flush_context, instances) -> None:
    """Hash-chains every new `DBAuditLog` row about to be flushed -- see `audit_chain.py`
    for the formula. `_audit()` (server.py) stamps a transient `_chain_seq` on each row
    at CREATION time so multiple audit rows within the same flush chain against EACH
    OTHER in creation order, not `session.new`'s unordered iteration. The advisory
    transaction lock forces two concurrent requests auditing at once to serialize the
    read-then-chain-then-insert sequence, rather than both forking off the same prior hash.
    """
    from .audit_chain import compute_chain_hash
    from .models import DBAuditLog, utcnow_naive

    new_rows = [obj for obj in session.new if isinstance(obj, DBAuditLog)]
    if not new_rows:
        return
    new_rows.sort(key=lambda r: getattr(r, "_chain_seq", 0))

    conn = session.connection()
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _AUDIT_CHAIN_LOCK_KEY})
    prev_row = conn.execute(
        text("SELECT chain_hash FROM audit_log ORDER BY id DESC LIMIT 1")
    ).first()
    prev_hash = prev_row[0] if prev_row else None

    for row in new_rows:
        if row.ts is None:
            row.ts = utcnow_naive()
        chain_hash = compute_chain_hash(
            prev_hash, row.ts, row.username, row.action, row.project_id,
            row.status_code, row.details,
        )
        row.chain_hash = chain_hash
        if row.signature is None:
            from . import audit_signing

            row.signature = audit_signing.sign(chain_hash)
        prev_hash = chain_hash


@event.listens_for(TenantScopedSession, "after_begin")
def _apply_tenant_guc(session, transaction, connection) -> None:
    """Re-applies the Postgres session GUC `app.tenant_id` on EVERY new transaction this
    session opens, not just the first -- some routes (`update_ref`, `prune_audit_log`/
    `prune_events`) open more than one Postgres transaction per HTTP request, and a
    one-shot `SET LOCAL` at session creation would silently stop applying after the
    first commit. A no-op whenever `session.info` carries no tenant."""
    tenant_id = session.info.get("tenant_id")
    if not TENANCY_ENFORCE or not tenant_id:
        return
    # set_config(), NOT a string-formatted `SET LOCAL app.tenant_id = ...` -- SET is a
    # utility statement and doesn't accept ordinary bind parameters over asyncpg's
    # extended query protocol; set_config() is an ordinary parameterized function call.
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),  # true = LOCAL, txn-scoped
        {"tid": str(tenant_id)},
    )


async_session_factory = sessionmaker(
    app_engine,
    class_=AsyncSession,
    sync_session_class=TenantScopedSession,
    expire_on_commit=False,
)

# A SEPARATE session factory for the two legitimately cross-tenant background workers
# (`_webhook_retry_worker`, `run_garbage_collection`) -- both must see every tenant's
# rows, which the tenant-scoped GUC above would otherwise restrict. Bypass is GUC-based
# (`app.bypass_rls`), not a second Postgres ROLE, since a dedicated BYPASSRLS role needs
# CREATEROLE/superuser privilege a managed Postgres app user won't always hold. Never
# exposed to any HTTP-facing FastAPI dependency.
system_session_factory = sessionmaker(
    engine,
    class_=AsyncSession,
    sync_session_class=TenantScopedSession,
    expire_on_commit=False,
)


@event.listens_for(TenantScopedSession, "after_begin")
def _apply_bypass_rls(session, transaction, connection) -> None:
    if not TENANCY_ENFORCE or not session.info.get("bypass_rls"):
        return
    connection.execute(text("SELECT set_config('app.bypass_rls', 'true', true)"))


MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Columns introduced after the create_all era began. Legacy databases may lack them;
# they are healed zero-touch at first boot and stamped onto the migration chain.
# tenant_id DDL is NOT NULL with a constant DEFAULT (not merely nullable like most other
# legacy columns here), since the real migration path makes this column NOT NULL at the
# schema level unconditionally -- a DEFAULT constant backfills every existing row of an
# adopted volume in the same ALTER TABLE statement, metadata-only and cheap on Postgres 11+.
_TENANT_ID_DDL = "VARCHAR NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001'"

_LEGACY_COLUMNS = {
    "commits": {"extra_parents": "TEXT", "signature": "TEXT",
                "env_snapshot_id": "TEXT", "tenant_id": _TENANT_ID_DDL},
    # objects/trees: tenant_id joins their PRIMARY KEY on the normal migration-chain path,
    # but this map can only ADD a column, never a constraint change -- an adopted volume
    # ends up with the tenant_id column present but the OLD, un-widened PRIMARY KEY.
    # Harmless today (no code path yet creates a second tenant_id for these two tables),
    # left as a documented, deliberate gap rather than in scope for this ADD-COLUMN tool.
    "objects": {"tenant_id": _TENANT_ID_DDL},
    "trees": {"chunks": "JSON", "tenant_id": _TENANT_ID_DDL},
    "refs": {"tenant_id": _TENANT_ID_DDL},
    "run_commits": {"tenant_id": _TENANT_ID_DDL},
    "events": {"tenant_id": _TENANT_ID_DDL},
    # chain_hash healed NULLABLE here; _heal_audit_chain_hash() below runs the real
    # backfill right after this ADD COLUMN, then sets NOT NULL itself, since a bare DDL
    # default can't express "each row's own computed hash".
    "audit_log": {"status_code": "INTEGER", "tenant_id": _TENANT_ID_DDL,
                  "chain_hash": "VARCHAR", "signature": "VARCHAR"},
    "webhooks": {
        "last_success_at": "TIMESTAMP",
        "last_failure_at": "TIMESTAMP",
        "consecutive_failures": "INTEGER DEFAULT 0",
        "disabled_reason": "TEXT",
        "tenant_id": _TENANT_ID_DDL,
    },
    "webhook_deliveries": {"tenant_id": _TENANT_ID_DDL},
    # New tables (improver_versions, change_sets, policy_packs, etc.) need no entry here
    # for their OWN columns -- a legacy volume lacking them entirely is handled by
    # `_create_missing_tables`; this map is only for columns added to an EXISTING table
    # (each of these tables needs a `tenant_id` entry since that column was added to an
    # existing table by a later migration).
    "runs": {"avh_object_id": "TEXT", "policy_outcome": "JSON",
             "kind": "TEXT DEFAULT 'train'", "improver_id": "TEXT",
             "integrity_signals": "JSON", "plan_id": "TEXT", "budget_id": "TEXT",
             "stop_reason": "TEXT", "lessons_id": "TEXT", "tenant_id": _TENANT_ID_DDL},
    "improver_versions": {"tenant_id": _TENANT_ID_DDL},
    "change_sets": {"tenant_id": _TENANT_ID_DDL},
    "policy_packs": {"tenant_id": _TENANT_ID_DDL},
    "canary_results": {"tenant_id": _TENANT_ID_DDL},
    "project_freeze": {"tenant_id": _TENANT_ID_DDL},
    "eval_suites": {"tenant_id": _TENANT_ID_DDL},
    "eval_results": {"tenant_id": _TENANT_ID_DDL},
    "eval_adapters": {"tenant_id": _TENANT_ID_DDL},
    "tasks": {"tenant_id": _TENANT_ID_DDL},
    "plans": {"tenant_id": _TENANT_ID_DDL},
    "budgets": {"tenant_id": _TENANT_ID_DDL},
    "causal_links": {"tenant_id": _TENANT_ID_DDL},
    "strategy_entries": {"tenant_id": _TENANT_ID_DDL},
    "lessons": {"tenant_id": _TENANT_ID_DDL},
    "reviews": {"tenant_id": _TENANT_ID_DDL},
    "critiques": {"tenant_id": _TENANT_ID_DDL},
    "blackboard_entries": {"tenant_id": _TENANT_ID_DDL},
    "sandbox_jobs": {"tenant_id": _TENANT_ID_DDL},
    "tool_manifests": {"tenant_id": _TENANT_ID_DDL},
    "action_logs": {"tenant_id": _TENANT_ID_DDL},
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


def _heal_audit_chain_hash(sync_conn, tables: set[str]) -> None:
    """Runs the same backfill migration `0016` runs, for a legacy volume adopted
    straight to head. `_heal_legacy_columns` above only gets `audit_log.chain_hash` into
    existence (nullable); this fills every row using the shared formula, then sets NOT
    NULL. A no-op on any volume that already has every row populated."""
    import sqlalchemy as sa

    from .audit_chain import compute_chain_hash

    if "audit_log" not in tables:
        return
    inspector = sa.inspect(sync_conn)
    columns = {c["name"] for c in inspector.get_columns("audit_log")}
    if "chain_hash" not in columns:
        return  # pre-migration-0016 shape entirely; nothing to backfill yet
    unpopulated = sync_conn.execute(
        sa.text("SELECT count(*) FROM audit_log WHERE chain_hash IS NULL")
    ).scalar_one()
    if not unpopulated:
        return

    rows = sync_conn.execute(
        sa.text(
            "SELECT id, ts, username, action, project_id, status_code, details "
            "FROM audit_log ORDER BY id ASC"
        )
    ).fetchall()
    prev_hash = None
    for row in rows:
        chain_hash = compute_chain_hash(
            prev_hash, row.ts, row.username, row.action, row.project_id,
            row.status_code, row.details,
        )
        sync_conn.execute(
            sa.text("UPDATE audit_log SET chain_hash = :chain_hash WHERE id = :row_id"),
            {"chain_hash": chain_hash, "row_id": row.id},
        )
        prev_hash = chain_hash
    # SET NOT NULL syntax is Postgres-specific; SQLite (test harness only, never a real
    # deployment) skips the constraint tightening -- values are still all populated.
    if sync_conn.dialect.name == "postgresql":
        sync_conn.exec_driver_sql("ALTER TABLE audit_log ALTER COLUMN chain_hash SET NOT NULL")


def _heal_legacy_indexes(sync_conn, tables: set[str]) -> None:
    """Creates any index a CURRENT table's model declares but an adopted legacy volume
    doesn't have -- the index-shaped sibling of `_heal_legacy_columns`. A migration that
    only adds `op.create_index(...)` with no matching column left an adopted volume
    silently missing those indexes forever, despite reporting itself at head. Walks
    every table `Base.metadata` declares, comparing against what's really on disk, and
    creates whatever's missing via the same `Index` object the model declared."""
    import sqlalchemy as sa

    from .models import Base

    inspector = sa.inspect(sync_conn)
    for table_name, table in Base.metadata.tables.items():
        if table_name not in tables:
            continue  # a genuinely missing table is _create_missing_tables' job, not this
        present = {ix["name"] for ix in inspector.get_indexes(table_name)}
        for index in table.indexes:
            if index.name not in present:
                index.create(bind=sync_conn)


def _create_missing_tables(sync_conn, tables: set[str]) -> None:
    """Creates any models.py tables a legacy volume doesn't have yet (idempotent).
    Adoption stamps the ENTIRE chain as applied, so later revisions never run on that
    volume -- a true legacy create_all database would otherwise be stamped to head and
    silently stay missing newer tables. Existing tables are never touched."""
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

    # Two engine replicas starting SIMULTANEOUSLY against the SAME brand-new database can
    # both race into `alembic upgrade head` at once: Alembic's own `CREATE TABLE IF NOT
    # EXISTS alembic_version` is NOT safe against a concurrent CREATE (existence check and
    # create are two separate steps), causing a startup-crashing UniqueViolationError.
    # `pg_advisory_xact_lock` forces every concurrent booter through this entire migration
    # run one at a time; the second replica's own subsequent run is then a cheap no-op.
    if sync_conn.dialect.name == "postgresql":
        sync_conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_MIGRATION_LOCK_KEY})

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
        # Tables the healing above just created (via create_all) already have their
        # indexes — re-inspect so this only ever considers indexes on tables that
        # already existed before adoption ran (create_all already made the rest).
        healed_tables = set(sa.inspect(sync_conn).get_table_names())
        _heal_legacy_indexes(sync_conn, healed_tables)
        # v1.3.3 (migration 0016): real per-row backfill, not a static DDL default --
        # must run AFTER _heal_legacy_columns put the (nullable) column in place.
        _heal_audit_chain_hash(sync_conn, healed_tables)
        MigrationContext.configure(sync_conn).stamp(script, script.get_current_head())
    elif _schema_is_ahead_of_this_binary(sync_conn, script):
        # A rolling upgrade puts an OLDER server binary against a database an already-
        # upgraded replica has migrated past this binary's own head -- a real, expected
        # state during any rolling deploy, not corruption. `command.upgrade(cfg, "head")`
        # below would otherwise crash startup outright (alembic can't resolve an unknown
        # revision). Skipping it is safe under VERSIONING.md's schema-compatibility
        # contract: new columns/tables are always additive and nullable/default-safe.
        import logging

        logging.getLogger(__name__).warning(
            "Database schema is at a revision this server binary's migration chain "
            "does not recognize (own head: %s) -- assuming a newer binary already "
            "migrated it during a rolling upgrade. Skipping `alembic upgrade head` "
            "rather than crashing startup; this instance will keep serving against the "
            "additive, backward-compatible schema its own code already knows. Upgrade "
            "this instance's image when convenient.",
            script.get_current_head(),
        )
        return

    cfg.attributes["connection"] = sync_conn
    command.upgrade(cfg, "head")


def _schema_is_ahead_of_this_binary(sync_conn, script) -> bool:
    """True when the database's recorded current revision is a real, non-null value
    that this binary's own alembic script directory does not contain -- some OTHER,
    newer binary has already migrated it past what this process knows about."""
    from alembic.runtime.migration import MigrationContext

    current_rev = MigrationContext.configure(sync_conn).get_current_revision()
    if current_rev is None:
        return False
    known_revisions = {rev.revision for rev in script.walk_revisions()}
    return current_rev not in known_revisions


def _unrecorded_chain(sync_conn, tables: set) -> bool:
    """True when the migration chain has no recorded revision for this database."""
    from alembic.runtime.migration import MigrationContext

    if "alembic_version" not in tables:
        return True
    return MigrationContext.configure(sync_conn).get_current_revision() is None


async def _apply_schema(target_engine: AsyncEngine) -> None:
    """Brings `target_engine`'s database to the latest migration. MUST be
    `engine.begin()`, not a plain `connect()` context: SQLAlchemy 2.0's commit-as-you-go
    means a plain connection rolls everything back at context exit, and Postgres
    honours that -- silently throwing the whole schema away. N replicas booting
    concurrently against the same fresh database all reach this function at once;
    `_ensure_schema_sync`'s own `pg_advisory_xact_lock` serializes them."""
    cfg = _alembic_config()
    async with target_engine.begin() as conn:
        await conn.run_sync(_ensure_schema_sync, cfg)


async def init_db() -> None:
    """Replaces the old create_all startup: schema is owned by Alembic from now on.
    Fresh databases walk the full migration chain; legacy databases are detected,
    healed of known column drift, and stamped to head. Failures fail startup loudly --
    a silently wrong schema would be worse than a down server."""
    await _apply_schema(engine)


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a single DB session per request. `request` is
    bare `Request`-typed (not `Optional[Request] = None`, which FastAPI's dependency
    resolution rejects) so every route picks it up with zero call-site changes. Never
    called outside FastAPI's dependency injection -- direct callers use
    `async_session_factory()`/`system_session_factory()` instead."""
    tenant_id = None
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        tenant_id = principal.tenant_id
    async with async_session_factory() as session:
        session.info["tenant_id"] = tenant_id
        yield session


async def get_system_session() -> AsyncGenerator[AsyncSession, None]:
    """The bypass-RLS session for the two legitimately cross-tenant background workers --
    never wired as a FastAPI `Depends()`, called directly instead."""
    async with system_session_factory() as session:
        session.info["bypass_rls"] = True
        yield session
