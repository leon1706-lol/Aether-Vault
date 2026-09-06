"""Tenant scoping, phase 2 of 2 — NOT NULL + FK + row-level security — v1.3.2

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-04

Completes what migration 0012 started: every tenant-scoped table's `tenant_id` becomes
NOT NULL with a real FK to `tenants(id)`, gains a composite index, and gets a Postgres
ROW LEVEL SECURITY policy — the database-level backstop behind the application-layer
guard (`server.py::_enforce_project_tenant`).

**Fail-closed, not fail-open.** An earlier draft read `GUC IS NULL OR tenant_id = GUC`
(permissive when unset — exactly backwards for a backstop); the policy below instead
COALESCEs an unset GUC to `DEFAULT_TENANT_ID`, so an unset GUC means "the one tenant
this deployment already had", never "no filter at all". Bypass for the two legitimately
cross-tenant background workers is GUC-based (`SET LOCAL app.bypass_rls = 'true'` via
`database.py`'s `system_session_factory`), not a second Postgres role, since this is a
backstop against application bugs, not a hard privilege boundary.

**Live-verified gap in this repo's own default deployment: RLS is currently INERT
under `docker-compose.yml`.** Postgres unconditionally exempts database SUPERUSERS from
RLS (a separate mechanism from the GUC bypass above, and `FORCE ROW LEVEL SECURITY`
cannot override it); `av_user` connects as a superuser only because the official
`postgres` Docker image grants that to whatever `POSTGRES_USER` names. Confirmed live
via a two-tenant test that found the policy correctly defined/enabled/forced yet still
non-filtering. Consequence: every explicit single-`project_id` route stays correctly
enforced by application-layer code regardless; every UNFILTERED list route needs its own
explicit tenant filter and cannot rely on RLS alone (`list_commits`/`list_projects` were
fixed directly; other unfiltered list routes remain a known, flagged residual gap). The
real fix — connecting as a dedicated non-superuser role — is an infrastructure change,
not a migration, and is deliberately not attempted here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_TENANT_SCOPED_TABLES = [
    "commits", "refs", "runs", "run_commits", "events", "webhooks", "webhook_deliveries",
    "audit_log", "improver_versions", "change_sets", "policy_packs", "canary_results",
    "project_freeze", "eval_suites", "eval_results", "eval_adapters", "tasks", "plans",
    "budgets", "causal_links", "strategy_entries", "lessons", "reviews", "critiques",
    "blackboard_entries", "sandbox_jobs", "tool_manifests", "action_logs",
]

_POLICY_SQL = """
CREATE POLICY tenant_isolation ON {table}
  USING (
    current_setting('app.bypass_rls', true) = 'true'
    OR tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '{default_tenant}')
  )
  WITH CHECK (
    current_setting('app.bypass_rls', true) = 'true'
    OR tenant_id = COALESCE(NULLIF(current_setting('app.tenant_id', true), ''), '{default_tenant}')
  )
"""
# CREATE POLICY rejects bind parameters over asyncpg's extended query protocol, so
# DEFAULT_TENANT_ID (a fixed internal constant, never external input) is inlined via
# .format() instead.


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TENANT_SCOPED_TABLES:
        op.alter_column(table, "tenant_id", nullable=False)
        op.create_foreign_key(f"fk_{table}_tenant_id", table, "tenants",
                              ["tenant_id"], ["id"])
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

        conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        # FORCE closes the "table owner bypasses RLS by default" hole -- without it this
        # migration is a no-op for the app's own primary connection.
        conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        conn.execute(sa.text(_POLICY_SQL.format(table=table, default_tenant=DEFAULT_TENANT_ID)))


def downgrade() -> None:
    conn = op.get_bind()
    for table in reversed(_TENANT_SCOPED_TABLES):
        conn.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        conn.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        conn.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_constraint(f"fk_{table}_tenant_id", table, type_="foreignkey")
        op.alter_column(table, "tenant_id", nullable=True)
