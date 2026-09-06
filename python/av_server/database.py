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

# v1.3.2 (migration 0015): the real fix for the RLS-superuser gap migration 0013's own
# docstring documented and deliberately deferred. `engine`/`DATABASE_URL` above (today's
# `av_user`) MUST keep having DDL rights — Alembic migrations and the two
# legitimately-cross-tenant background workers (`system_session_factory`) both still use
# it unconditionally. `AV_APP_DATABASE_URL` is a SEPARATE, OPTIONAL connection string for
# ordinary request-serving sessions only, meant to point at the new `av_app` role
# (migration 0015 grants it exactly SELECT/INSERT/UPDATE/DELETE, no DDL, no
# SUPERUSER/BYPASSRLS) — a role Postgres does NOT exempt from row-level security, unlike
# `av_user`. Unset (the default for any deployment that hasn't opted in) means
# `async_session_factory` below keeps using the exact same `engine` it always has —
# byte-identical, per guardrail #1. `docker-compose.yml` sets this repo's own default
# topology to use it, which is what actually closes 0013's documented gap THERE
# specifically; it remains off for anyone who upgrades without changing their compose
# file or env.
APP_DATABASE_URL: str | None = os.environ.get("AV_APP_DATABASE_URL") or None
app_engine: AsyncEngine = (
    create_async_engine(APP_DATABASE_URL, echo=False, pool_pre_ping=True)
    if APP_DATABASE_URL
    else engine
)

# v1.3.2 enterprise tenancy: kept in sync with models.py::DEFAULT_TENANT_ID by a
# same-string-literal contract (importing models here would be circular — server.py
# already imports FROM this module). Both call sites are covered by
# tests/test_tenancy.py::test_database_default_tenant_matches_models_constant.
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# Whether row-level security is actually ENFORCED (the GUC below is set at all). Off by
# default — VERSIONING.md's MINOR-release contract: an unconfigured deployment must
# behave byte-identically to pre-v1.3.2. `tenant_id` is still ALWAYS populated on every
# insert regardless of this flag (`_populate_tenant_id` below) — that part is not
# optional, since the column is NOT NULL at the schema level unconditionally; only the
# GUC-setting / actual filtering is gated.
TENANCY_ENFORCE: bool = os.environ.get("AV_TENANCY_ENFORCE", "0") == "1"


class TenantScopedSession(Session):
    """A dedicated Session subclass (not the bare `Session` class) so the event
    listeners below are scoped to av_server's own session factories only — never firing
    for some other SQLAlchemy Session that might exist in-process (a test harness's
    own, for instance)."""


@event.listens_for(TenantScopedSession, "before_flush")
def _populate_tenant_id(session, flush_context, instances) -> None:
    """Every newly-inserted row on a tenant-scoped model (any model with a `tenant_id`
    attribute — models.py's 28 tenant-scoped tables) gets one, UNCONDITIONALLY: this is
    not gated by TENANCY_ENFORCE, because the column is NOT NULL at the schema level
    regardless of that flag (migration 0013). Falls back to DEFAULT_TENANT_ID exactly
    like every pre-existing row was backfilled to — an unconfigured deployment's writes
    land in the same one tenant its reads already see.

    This is what keeps all ~30 `db.add(DBXxx(...))` call sites across server.py
    UNCHANGED — none of them needs to know about tenancy at all. Found necessary live
    (not designed up front): the first real migration-0013 test run hit a NOT NULL
    violation on `audit_log.tenant_id` from `_audit()`'s existing, untouched call site,
    which is exactly the class of call site this listener exists to cover without
    editing.
    """
    tenant_id = session.info.get("tenant_id") or DEFAULT_TENANT_ID
    for obj in session.new:
        if hasattr(obj, "tenant_id") and getattr(obj, "tenant_id", None) is None:
            obj.tenant_id = tenant_id


# v1.3.3 (WP-32, migration 0016): a fixed, arbitrary lock key -- any stable int works,
# this one has no special meaning. Scopes `pg_advisory_xact_lock` to exactly this one
# purpose so it can never contend with some unrelated advisory lock elsewhere.
_AUDIT_CHAIN_LOCK_KEY = 881736452901

# v1.3.3.2 (HA drill, found live): a SEPARATE fixed key for serializing schema
# migrations across N replicas booting concurrently against the SAME fresh database --
# see `_apply_schema`'s own docstring for the actual incident this fixes.
_SCHEMA_MIGRATION_LOCK_KEY = 881736452902


@event.listens_for(TenantScopedSession, "before_flush")
def _chain_audit_log(session, flush_context, instances) -> None:
    """Hash-chains every new `DBAuditLog` row about to be flushed — see
    `audit_chain.py` for the formula and migration `0016`'s docstring for the full
    design (why there's no `prev_id` column, and the advisory-lock concurrency story).

    `_audit()` (server.py) stamps a transient `_chain_seq` on each row at CREATION time
    (not flush time) so multiple audit rows added within the same flush chain against
    EACH OTHER in the order they were created, not `session.new`'s unordered iteration —
    a single request that calls `_audit()` twice (a handful of routes do) must not
    silently chain them in the wrong order.

    Real, not hypothetical, concurrency risk this specifically guards against: two
    separate requests both auditing at once could otherwise both read the same "last
    chain_hash" and both compute a hash chained from it — a genuine fork. The advisory
    transaction lock (`pg_advisory_xact_lock`, auto-released at commit/rollback) forces
    any two concurrent transactions doing this to serialize for exactly this read-then-
    chain-then-insert sequence, and nothing else in either transaction is affected.
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
    session opens — not just the first. This is the fix for a real hazard, verified by
    reading the actual route bodies before writing this: `update_ref` (an early-exit
    commit-and-raise on a lost race, or fall-through to update+commit on success) and
    `prune_audit_log`/`prune_events` (each commit twice) all open more than one
    Postgres transaction within a single HTTP request. A one-shot `SET LOCAL` at session
    creation would silently stop applying at the first of those commits — the worst
    failure mode, since enforcement would look on and be off. `after_begin` is tied to
    `Session.begin()`, which SQLAlchemy calls transparently on first use AND again after
    every commit/rollback followed by further use — "once per transaction", exactly the
    granularity this needs.

    A no-op (network round trip skipped entirely, not just a no-op result) whenever
    `session.info` carries no tenant — Anonymous mode, TENANCY_ENFORCE off, or a
    background-worker session using `system_session_factory` instead (see below).
    """
    tenant_id = session.info.get("tenant_id")
    if not TENANCY_ENFORCE or not tenant_id:
        return
    # set_config(), NOT a string-formatted `SET LOCAL app.tenant_id = ...` -- SET is a
    # utility statement and does not accept ordinary bind parameters over asyncpg's
    # extended query protocol (the exact class of bug migration 0013's CREATE POLICY
    # hit live — DDL/utility statements reject bind params outright there; set_config()
    # sidesteps this identically to how that migration's own fix does, by being an
    # ordinary parameterized function call instead).
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

# v1.3.2: a SEPARATE session factory for the two legitimately cross-tenant background
# workers (`_webhook_retry_worker`, `run_garbage_collection`, server.py) — both must see
# every tenant's rows, which the tenant-scoped GUC above would otherwise restrict once
# TENANCY_ENFORCE is on. Bypass is GUC-based (`app.bypass_rls`, checked by every RLS
# policy migration 0013 creates), not a second Postgres ROLE — see migration 0013's own
# module docstring for why a dedicated BYPASSRLS role was rejected (it needs
# CREATEROLE/superuser privilege a real managed Postgres deployment's app user will not
# always hold). Never exposed to any HTTP-facing FastAPI dependency.
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

# Columns introduced after the create_all era began. Legacy databases (created by the
# pre-Alembic startup) may lack them; they are healed zero-touch at first boot with the
# Alembic adoption and then stamped onto the migration chain. Kept in sync with the
# additive migrations: 0002-era drift is healed for volumes adopted before 0003 existed;
# 0003 adds audit_log.status_code and commits.signature; 0004 adds webhook health
# tracking columns and runs.avh_object_id.
# v1.3.2 (migrations 0012/0013): tenant_id DDL shared by every entry below — NOT NULL
# with a constant DEFAULT (not merely nullable, unlike most other legacy columns here),
# because the real migration path makes this column NOT NULL at the schema level
# unconditionally (guardrail: `tenant_id` is always populated regardless of
# AV_TENANCY_ENFORCE, since the column itself is never optional). A DEFAULT constant
# backfills every existing row of an adopted volume in the same ALTER TABLE statement —
# metadata-only and cheap on Postgres 11+, no separate UPDATE pass needed here the way
# the real migration 0012 needs one (that migration walks tables genuinely large enough
# to want batching; a column-heal ALTER on an already-departed legacy volume is not
# expected to be that large, and if it is, is a one-time cost at that volume's very next
# boot, not a recurring one).
_TENANT_ID_DDL = "VARCHAR NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001'"

_LEGACY_COLUMNS = {
    "commits": {"extra_parents": "TEXT", "signature": "TEXT",
                "env_snapshot_id": "TEXT", "tenant_id": _TENANT_ID_DDL},
    # objects/trees (migration 0014): tenant_id joins their PRIMARY KEY on the normal
    # migration-chain path, not just a plain column — this map can only ADD a column
    # (_heal_legacy_columns is a bare `ALTER TABLE ADD COLUMN`, never a constraint
    # change), so a volume healed via the adoption path (never touched migration 0014's
    # own DROP/ADD CONSTRAINT DDL) ends up with the tenant_id COLUMN present and
    # NOT-NULL-satisfied, but its objects/trees PRIMARY KEY stays the OLD bare-hash /
    # (tree_hash, path_name) shape — a real, narrow inconsistency versus a
    # freshly-migrated volume, left as-is deliberately: harmless today (no code path
    # yet creates a second tenant_id value for these two tables at all, so the
    # un-widened PK still uniquely identifies every row), and re-widening a PRIMARY KEY
    # from inside adoption-healing is out of scope for the ADD-COLUMN-shaped tool this
    # map already is. Documented here rather than silently left unconsidered.
    "objects": {"tenant_id": _TENANT_ID_DDL},
    "trees": {"chunks": "JSON", "tenant_id": _TENANT_ID_DDL},
    "refs": {"tenant_id": _TENANT_ID_DDL},
    "run_commits": {"tenant_id": _TENANT_ID_DDL},
    "events": {"tenant_id": _TENANT_ID_DDL},
    # chain_hash healed NULLABLE here (unlike migration 0016's real NOT NULL) --
    # _heal_audit_chain_hash() below runs the real backfill computation right after this
    # ADD COLUMN, then sets NOT NULL itself. A bare DDL default (like _TENANT_ID_DDL's
    # constant) cannot express "each row's own computed hash", so this map only gets the
    # column INTO existence; the actual values come from that dedicated heal step.
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
    # 0006 (v1.3.1): RSI R1 — runs.kind/improver_id. New tables (improver_versions,
    # change_sets, policy_packs, canary_results, project_freeze) need no entry here for
    # THEIR OWN columns — a legacy volume simply lacks them entirely, which
    # `_create_missing_tables` already handles from Base.metadata; this map is only for
    # columns added to an EXISTING table. They DO need a `tenant_id` entry below,
    # though — that column was added to THEM by migration 0012, an existing-table
    # change, the exact case this map exists for, once these tables themselves already
    # exist on a volume with lost version tracking (see this dict's own module comment
    # on why the adoption path cannot assume which subset of tables such a volume has).
    # 0007 (v1.3.1): RSI R2 — runs.integrity_signals. Same rationale for eval_suites/
    # eval_results/eval_adapters/tasks.
    # 0008 (v1.3.1): RSI R3 — runs.plan_id/budget_id/stop_reason. Same rationale for
    # plans/budgets.
    # 0009 (v1.3.1): RSI R4 — runs.lessons_id. Same rationale for causal_links/
    # strategy_entries/lessons/reviews/critiques/blackboard_entries.
    # 0010 (v1.3.1): RSI R5 — sandbox_jobs/tool_manifests/action_logs, same rationale.
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
    """Runs the SAME backfill migration `0016` runs, for a legacy volume adopted
    straight to head (never walking through `0016` itself). `_heal_legacy_columns`
    above only gets `audit_log.chain_hash` INTO existence (nullable, no computed
    values) — this fills every row for real using the one shared formula
    (`audit_chain.compute_chain_hash`), then sets NOT NULL, matching what a real
    step-by-step migration run would have produced. A no-op (fast column-presence
    check only) on any volume that already has every row populated."""
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
    # SET NOT NULL syntax is Postgres-specific (SQLite has no equivalent ALTER COLUMN
    # form) -- this codebase's real deployments are always Postgres; SQLite here is
    # only ever this test file's isolated healing-logic harness, never a serving DB, so
    # skipping the constraint tightening there (column stays nullable at the SQLite
    # level, values are still all populated) is a safe, honest trade-off, not a gap that
    # matters in practice.
    if sync_conn.dialect.name == "postgresql":
        sync_conn.exec_driver_sql("ALTER TABLE audit_log ALTER COLUMN chain_hash SET NOT NULL")


def _heal_legacy_indexes(sync_conn, tables: set[str]) -> None:
    """Creates any index a CURRENT table's model declares but an adopted legacy volume
    doesn't have — the index-shaped sibling of `_heal_legacy_columns` (v1.3.0).

    Real gap this closes: a volume adopted via the `needs_adoption` branch gets its
    COLUMNS healed (`_LEGACY_COLUMNS`, matched by hand) and stamped straight to head —
    but nothing ever diffed INDEXES the same way. A migration that only adds
    `op.create_index(...)` with no matching column (migration 0004's
    `ix_audit_log_username`/`ix_audit_log_action`) left an adopted volume silently
    missing those indexes forever, even though it reports itself as being at head — found
    via `tests/test_server.py::test_migration_chain_downgrades_and_reupgrades_cleanly`
    hitting `UndefinedObjectError` trying to DROP an index that a REAL step-by-step
    upgrade would have created, but this environment's adopted volume never had.

    Walks every table `Base.metadata` (the models — the single source of truth an
    adopted volume is diffed against) actually declares, comparing against what
    `sa.inspect` reports is really on disk; creates whatever's missing via the same
    `Index` object the model declared, so the DDL is byte-identical to what a fresh
    `create_all()` would have produced.
    """
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

    # v1.3.3.2 fix (found live via the HA drill, real Postgres, real concurrent boot —
    # not a hypothetical): two engine replicas starting SIMULTANEOUSLY against the SAME
    # brand-new database both raced into `alembic upgrade head` at once. Alembic's own
    # `_ensure_version_table()` issues `CREATE TABLE IF NOT EXISTS alembic_version` --
    # which is NOT safe against a concurrent CREATE of the same table: Postgres's
    # existence check and the actual create are two separate steps, so two sessions can
    # both see "doesn't exist" and both attempt to create it, with one losing to
    # `UniqueViolationError: duplicate key value violates unique constraint
    # "pg_type_typname_nsp_index"` (the table's own row TYPE collides in the shared
    # `pg_type` catalog) — a startup crash, not a benign race. `pg_advisory_xact_lock`
    # (auto-released at this transaction's commit/rollback, same pattern
    # `_chain_audit_log` already uses for a different serialization need) forces every
    # concurrent booter through this ENTIRE migration run one at a time: the second
    # replica blocks here until the first's transaction commits, then runs its own
    # `alembic upgrade head` against an already-current database, which is a real, cheap
    # no-op — not skipped, genuinely verified current every time.
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
        # v1.3.4 (todo.md item 39, W3g): a rolling upgrade puts an OLDER server binary
        # against a database an ALREADY-upgraded replica has migrated past this binary's
        # own head — a real, expected state during any rolling deploy (docs/runbooks/
        # upgrade-rollback.md), not a corruption. Before this fix, `command.upgrade(cfg,
        # "head")` below crashed startup outright: alembic must resolve the database's
        # CURRENT revision within ITS OWN script directory to compute an upgrade path,
        # and a revision this binary has never heard of raises
        # `alembic.util.exc.CommandError: Can't locate revision identified by '<rev>'`
        # unconditionally -- there is no code path where an unrecognized current revision
        # upgrades cleanly. Skipping the upgrade call entirely (rather than attempting
        # anything against a schema state this binary doesn't understand) is safe under
        # VERSIONING.md's own schema-compatibility contract: new columns/tables are
        # always additive and nullable/default-safe, so an older binary's unchanged ORM
        # models simply never reference whatever's new and keep working against
        # everything they DO know about.
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
    that this binary's OWN alembic script directory does not contain — i.e. some OTHER,
    newer binary has already migrated this database past what this process knows about.
    Distinct from `_unrecorded_chain()` (no revision recorded at all, the true-legacy
    case, handled by the `needs_adoption` branch above) — this is "a revision IS
    recorded, it's simply unknown here"."""
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
    """Brings `target_engine`'s database to the latest migration.

    MUST be `engine.begin()`, not a plain `connect()` context: SQLAlchemy 2.0's
    commit-as-you-go means a plain connection rolls everything back at context exit,
    and Postgres (unlike the pysqlite driver, whose DDL auto-commits) honours that —
    the v1.1.6–v1.1.8 CI runs executed every migration statement faithfully and then
    threw the whole schema away, silently. See Probleme.md #70.

    v1.3.3.2: N replicas booting concurrently against the SAME fresh database (the HA
    topology this repo actually ships, `docker-compose.ha.yml`) all reach this function
    at once — `_ensure_schema_sync`'s own `pg_advisory_xact_lock` (held for this whole
    `engine.begin()` transaction) is what serializes them, found live by the HA drill
    crashing BOTH engine replicas on first real concurrent boot with
    `UniqueViolationError` on `alembic_version`'s own catalog row.
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


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a single DB session per request.

    `request` is new (was previously argument-less) and backward compatible with every
    existing `Depends(get_session)` call site with zero changes at the call site: FastAPI
    always auto-injects the current `Request` for a dependency parameter type-hinted
    bare `Request` (an `Optional[Request] = None` signature was tried first and
    rejected — FastAPI's own dependency-resolution machinery tries to build a Pydantic
    response field for a dependency's non-`Request`-shaped parameters and errors out on
    `Request | None` specifically, found by actually importing the module, not just
    reading FastAPI's docs), so every one of the ~120 existing routes picks this up with
    zero call-site changes. This function is never called outside FastAPI's dependency
    injection anywhere in this codebase (grepped) — every direct/non-request caller
    already uses `async_session_factory()`/`system_session_factory()` directly instead.
    """
    tenant_id = None
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        tenant_id = principal.tenant_id
    async with async_session_factory() as session:
        session.info["tenant_id"] = tenant_id
        yield session


async def get_system_session() -> AsyncGenerator[AsyncSession, None]:
    """The bypass-RLS session for the two legitimately cross-tenant background workers
    (`_webhook_retry_worker`, `run_garbage_collection`) — never wired as a FastAPI
    `Depends()`, called directly by those two call sites in server.py only."""
    async with system_session_factory() as session:
        session.info["bypass_rls"] = True
        yield session
