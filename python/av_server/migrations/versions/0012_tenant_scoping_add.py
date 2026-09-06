"""Tenant scoping, phase 1 of 2 — add tenant_id (nullable) + backfill — v1.3.2

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-04

Split from the NOT-NULL/FK/RLS phase (migration 0013) deliberately -- the standard
Postgres 3-phase pattern for widening a large live table, so no single transaction holds
a long lock across add-column + backfill + constrain + RLS-enable.

Every column added here is NULLABLE with no default and backfilled in a SEPARATE
statement immediately after -- every pre-existing row lands in `DEFAULT_TENANT_ID`,
making an unconfigured deployment behave byte-identically after upgrading.

`objects`/`trees` are NOT touched here -- they get a different, PRIMARY-KEY-widening
treatment in migration 0014, since a tenant_id column alone would silently corrupt the
global content-addressed dedup contract those two tables provide.
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

# Batched backfill for tables that can be genuinely large in a live deployment, chunked
# like GC's own delete loop, so one backfill statement never holds a lock across an
# unbounded row count. The PK column is hardcoded from models.py rather than discovered
# via `sa.inspect(conn)`, since Alembic's offline (`--sql`) mode has no schema-reflection
# capability at all and would break the offline DDL-rendering test.
_BATCH_SIZE = 5000
_LARGE_TABLE_PKS = {
    "commits": "hash", "events": "id", "audit_log": "id",
    "webhook_deliveries": "id", "action_logs": "id",
}


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TENANT_SCOPED_TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.String(), nullable=True))

    # Offline (`--sql`) mode's MockConnection never returns a meaningful `.rowcount`, so
    # a `while ... rowcount == 0: break` loop would hang or misfire there. Offline mode
    # renders each batched statement exactly once instead of looping.
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
