"""Tenant scoping, phase 1 of 2 — add tenant_id (nullable) + backfill — v1.3.2

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-04

Split from the NOT-NULL/FK/RLS phase (migration 0013) deliberately — the standard
Postgres 3-phase pattern for widening a large live table, so no single transaction here
holds a long lock across add-column + backfill + constrain + RLS-enable on tables the
size `audit_log`/`events` can reach in a real deployment. See
development/architecture.md's Tenancy Isolation contract section.

Every column added here is NULLABLE with no default and is backfilled in a SEPARATE
statement immediately after — every pre-existing row across every table lands in
`DEFAULT_TENANT_ID` (models.py), the same well-known UUID migration 0011 seeded. This is
what makes an unconfigured, pre-v1.3.2 deployment behave byte-identically after
upgrading: there is exactly one tenant, and every row already belongs to it.

`objects`/`trees` are NOT touched here — they get a different, PRIMARY-KEY-widening
treatment (per-tenant CAS storage) in migration 0014, not a same-shape tenant_id column,
because they are the one place a tenant_id column alone would silently corrupt the
GLOBAL content-addressed dedup contract those two tables exist to provide.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# Every existing table that carries (or, for run_commits/refs, implicitly needs) a
# tenant boundary. objects/trees excluded — see module docstring.
_TENANT_SCOPED_TABLES = [
    "commits", "refs", "runs", "run_commits", "events", "webhooks", "webhook_deliveries",
    "audit_log", "improver_versions", "change_sets", "policy_packs", "canary_results",
    "project_freeze", "eval_suites", "eval_results", "eval_adapters", "tasks", "plans",
    "budgets", "causal_links", "strategy_entries", "lessons", "reviews", "critiques",
    "blackboard_entries", "sandbox_jobs", "tool_manifests", "action_logs",
]

# Batched backfill for tables that can be genuinely large in a live deployment — chunked
# the same way GC's own delete loop is (_GC_DELETE_BATCH, server.py), so one backfill
# statement never holds a lock across an unbounded row count. Every other table backfills
# in one UPDATE (small, bounded by nature — e.g. one row per project for project_freeze).
#
# The batched tables' PK column is hardcoded here (from models.py, the source of truth),
# NOT discovered via `sa.inspect(conn)` — found live: Alembic's offline (`--sql`) render
# mode runs every migration against a `MockConnection` that has no schema-reflection
# capability at all (`sa.inspect()` raises `NoInspectionAvailable` immediately), which
# would silently break `tests/test_migrations.py`'s offline DDL-rendering proof — the one
# stack-free test that exists specifically to catch a bad op-level call before any real
# database is involved. A hardcoded map is also strictly faster (no reflection round
# trip) and works identically online and offline.
_BATCH_SIZE = 5000
_LARGE_TABLE_PKS = {
    "commits": "hash", "events": "id", "audit_log": "id",
    "webhook_deliveries": "id", "action_logs": "id",
}


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TENANT_SCOPED_TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.String(), nullable=True))

    # Alembic's offline (`--sql`) mode executes every `conn.execute()` here against a
    # `MockConnection` that renders DDL/DML text but never returns a real, meaningful
    # `.rowcount` — a `while True: ... if result.rowcount == 0: break` loop driven by
    # that value would either hang forever or terminate on the wrong iteration when
    # rendering offline. `context.is_offline_mode()` is the documented way to tell the
    # two apart; offline mode renders each batched statement exactly ONCE (enough to
    # prove the SQL shape in `tests/test_migrations.py`'s offline DDL test), never loops.
    offline = context.is_offline_mode()
    for table in _TENANT_SCOPED_TABLES:
        pk = _LARGE_TABLE_PKS.get(table)
        if pk:
            stmt = sa.text(
                f"UPDATE {table} SET tenant_id = :tid "
                f"WHERE {pk} IN (SELECT {pk} FROM {table} WHERE tenant_id IS NULL LIMIT :batch)"
            )
            if offline:
                conn.execute(stmt, {"tid": DEFAULT_TENANT_ID, "batch": _BATCH_SIZE})
            else:
                while True:
                    result = conn.execute(stmt, {"tid": DEFAULT_TENANT_ID, "batch": _BATCH_SIZE})
                    if result.rowcount == 0:
                        break
        else:
            conn.execute(sa.text(f"UPDATE {table} SET tenant_id = :tid WHERE tenant_id IS NULL"),
                        {"tid": DEFAULT_TENANT_ID})


def downgrade() -> None:
    for table in reversed(_TENANT_SCOPED_TABLES):
        op.drop_column(table, "tenant_id")
