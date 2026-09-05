"""Tenant scoping, phase 2 of 2 — NOT NULL + FK + row-level security — v1.3.2

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-04

Completes what migration 0012 started: every tenant-scoped table's `tenant_id` becomes
NOT NULL with a real FK to `tenants(id)`, gains a composite index, and gets a Postgres
ROW LEVEL SECURITY policy — the database-level backstop behind the application-layer
guard (`server.py::_enforce_project_tenant`, a later phase). A route that FORGETS its
guard still returns zero cross-tenant rows once this is live and `AV_TENANCY_ENFORCE=1`.

**The policy is fail-closed, not fail-open — read this before touching it.** An earlier
draft of this policy (caught in design review, before any code existed) read `GUC IS
NULL OR tenant_id = GUC`: permissive whenever the tenant GUC was unset, meaning a route
that forgot to resolve a tenant would see EVERY tenant's rows — exactly backwards for a
backstop. The policy below instead COALESCEs an unset GUC to `DEFAULT_TENANT_ID`
(models.py) — since every pre-existing row across every table was backfilled to that
exact tenant (migration 0012), an unset GUC now means "exactly what this deployment
already saw before tenancy existed", not "no filter at all". This is what makes
enabling RLS here byte-identical for every unconfigured deployment while still being a
hard, real filter the moment a second tenant exists.

**Bypass is GUC-based, not a second Postgres role.** The two background workers that are
legitimately cross-tenant by design (`_webhook_retry_worker`, `run_garbage_collection`)
need to see every tenant's rows. The original design considered a dedicated
`CREATE ROLE av_migrator BYPASSRLS`, but that requires CREATEROLE/superuser privilege the
app's DB user will not always hold on a real managed Postgres instance — this migration
instead adds a second GUC, `app.bypass_rls`, checked by the same policy. A session sets
`SET LOCAL app.bypass_rls = 'true'` (via `database.py`'s dedicated
`system_session_factory`, never exposed to any HTTP-facing dependency) to see every
tenant's rows. This is intentionally a software boundary, not a hard database privilege
boundary: RLS here is a backstop against APPLICATION BUGS (a route forgetting its own
tenant guard), not a defense against a fully compromised app process with arbitrary SQL
execution — which could bypass any in-process authorization mechanism regardless of how
this policy is written.

**A real, live-verified gap in this repo's OWN default deployment: RLS is currently
INERT under `docker-compose.yml`.** Postgres unconditionally exempts database
SUPERUSERS from row-level security — no `FORCE ROW LEVEL SECURITY` can override that
exemption, and it is a completely separate mechanism from the GUC-based bypass two
paragraphs up. `docker-compose.yml`'s `av_user` connects as a SUPERUSER, because the
official `postgres` Docker image automatically grants superuser to whatever
`POSTGRES_USER` names — this repo never deliberately chose that, it is simply what the
image does by default and nothing here has ever needed to care until now. Found live —
a real two-tenant test (`tests/test_server.py::TestHardTenancy::
test_rls_transparently_filters_unfiltered_list_routes`) against this exact deployment
confirmed the policy is correctly enabled/forced/defined via `pg_class`/`pg_policy`
introspection, and STILL did not filter a query. This means:
- Every route with an explicit single `project_id` target is still correctly enforced —
  `server.py::_enforce_project_tenant`'s 403/404 denials are application-layer code,
  entirely unaffected by whether RLS itself is active.
- Every UNFILTERED list route (`GET /api/commits` with no `?project_id=`, and similarly
  shaped routes this phase did not individually audit) needs its OWN explicit tenant
  filter to be correct under this topology — RLS cannot be relied on alone. `list_commits`
  and `list_projects` (server.py) were fixed directly; the remaining unfiltered list
  routes are a known, explicitly flagged residual gap, not silently unfixed.
- The real, general fix is an INFRASTRUCTURE change — connect as a dedicated
  non-superuser role — not a migration. Deliberately not attempted here: altering an
  already-connected role's own superuser attribute live, against a shared pool with
  other open connections, is exactly the kind of infrastructure change that needs its
  own careful rollout (new role, ownership transfer, `DATABASE_URL` update, a fresh
  connection pool), not a rushed mid-migration fix. RLS remains genuinely valuable
  defense-in-depth for any deployment that already connects as a non-superuser role (a
  common, deliberate production pattern), and this policy is written correctly for that
  case — it just is not this repo's own current default.
this policy is written. Documented here plainly rather than implied.
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
# CREATE POLICY is DDL/a utility statement, not SELECT/INSERT/UPDATE/DELETE — asyncpg's
# extended query protocol REJECTS a bind parameter here outright ("the server expects 0
# arguments for this query"), found live (this exact migration, first real run against
# Postgres). DEFAULT_TENANT_ID is a fixed internal constant this module defines, never
# external input, so inlining it as a literal via .format() carries no injection
# surface — the same judgment call op.execute(f"...") elsewhere in this codebase's own
# migrations already makes for table/column names, which are never user-suppliable
# either.


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TENANT_SCOPED_TABLES:
        op.alter_column(table, "tenant_id", nullable=False)
        op.create_foreign_key(f"fk_{table}_tenant_id", table, "tenants",
                              ["tenant_id"], ["id"])
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

        conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        # FORCE is what closes the "the app connects AS the table owner, and owners
        # bypass RLS by default" hole — without it this whole migration would be a no-op
        # for the app's own primary connection, the one caller that matters most.
        conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        # sa.text(), not exec_driver_sql(): Alembic's offline (--sql) mode runs every
        # upgrade() against a MockConnection that implements .execute() but has no
        # exec_driver_sql() at all (AttributeError, found live) — .execute(sa.text(...))
        # is the one call shape that renders correctly in both offline and online modes,
        # matching the ENABLE/FORCE ROW LEVEL SECURITY calls directly above it.
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
