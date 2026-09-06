"""Non-superuser app role — closes the RLS-superuser gap documented in 0013 — v1.3.2

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-05

Migration 0013 flagged that RLS is INERT under this repo's own default
`docker-compose.yml` because `av_user` is a database SUPERUSER (granted automatically by
the official `postgres` image), and superusers are unconditionally exempt from RLS. This
migration is the schema half of that fix's careful rollout.

**Creates** a second Postgres role, `av_app` -- ordinary LOGIN, no
SUPERUSER/BYPASSRLS/CREATEROLE/CREATEDB -- granted SELECT/INSERT/UPDATE/DELETE on every
table and USAGE/SELECT on every sequence, via `ALTER DEFAULT PRIVILEGES` so future
migrations' tables are covered automatically. **Does not by itself** make anything
connect as `av_app`: `database.py`'s optional `AV_APP_DATABASE_URL` (same commit) routes
ordinary request-serving sessions through it when set, while migrations and the two
cross-tenant background workers keep using `DATABASE_URL`/`av_user`. Any deployment that
never sets the new env var keeps today's exact behavior.

Password is a fixed literal (matching `av_password`'s existing posture), inlined because
`CREATE ROLE ... PASSWORD` rejects bind parameters the same way `CREATE POLICY` did in
0013; a real deployment is expected to rotate it.

Idempotent (`IF NOT EXISTS` guard); `downgrade()` revokes every grant but deliberately
does NOT `DROP ROLE` -- see its own docstring.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "av_app"
# Fixed dev credential matching docker-compose.yml's `av_password` posture. See module docstring.
APP_ROLE_PASSWORD = "av_app_password"

_CREATE_ROLE_SQL = f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{APP_ROLE}') THEN
    CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_ROLE_PASSWORD}'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
  END IF;
END
$$;
"""

# Dynamic SQL via current_database() rather than a hardcoded literal, so this works
# unmodified against any database name.
_GRANT_CONNECT_SQL = f"""
DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO {APP_ROLE}', current_database());
END
$$;
"""

_REVOKE_CONNECT_SQL = f"""
DO $$
BEGIN
  EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {APP_ROLE}', current_database());
END
$$;
"""

# Uses CURRENT_USER (the role actually running this migration), not a hardcoded
# "av_user" literal -- found live: CI's fresh PostgreSQL provisions only a `postgres`
# superuser with no `av_user` role, and the hardcoded literal raised
# UndefinedObjectError on every fixture setup.
_ALTER_DEFAULT_GRANT_TABLES_SQL = f"""
DO $$
BEGIN
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}',
    CURRENT_USER
  );
END
$$;
"""

_ALTER_DEFAULT_GRANT_SEQUENCES_SQL = f"""
DO $$
BEGIN
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}',
    CURRENT_USER
  );
END
$$;
"""

_ALTER_DEFAULT_REVOKE_SEQUENCES_SQL = f"""
DO $$
BEGIN
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}',
    CURRENT_USER
  );
END
$$;
"""

_ALTER_DEFAULT_REVOKE_TABLES_SQL = f"""
DO $$
BEGIN
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}',
    CURRENT_USER
  );
END
$$;
"""

_GRANT_STATEMENTS = [
    f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}",
    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
    # Future tables/sequences: anything the migrating role creates in a LATER migration
    # is automatically granted to av_app with no per-migration maintenance.
    _ALTER_DEFAULT_GRANT_TABLES_SQL,
    _ALTER_DEFAULT_GRANT_SEQUENCES_SQL,
]

_REVOKE_STATEMENTS = [
    _ALTER_DEFAULT_REVOKE_SEQUENCES_SQL,
    _ALTER_DEFAULT_REVOKE_TABLES_SQL,
    f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}",
    f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}",
    f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}",
]


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(_CREATE_ROLE_SQL))
    conn.execute(sa.text(_GRANT_CONNECT_SQL))
    for stmt in _GRANT_STATEMENTS:
        conn.execute(sa.text(stmt))


def downgrade() -> None:
    """Revokes every grant this database's upgrade() made -- deliberately does NOT
    `DROP ROLE`, since `av_app` is a cluster-wide Postgres role that a real cluster may
    host multiple databases against; `DROP ROLE` fails whenever the role holds a grant
    in ANY other database, which a per-database migration can't see or safely touch.
    Net effect: an empty, privilege-less role is left behind. An operator who wants it
    gone cluster-wide runs `DROP ROLE av_app;` by hand once no database uses it.
    """
    conn = op.get_bind()
    for stmt in _REVOKE_STATEMENTS:
        conn.execute(sa.text(stmt))
    conn.execute(sa.text(_REVOKE_CONNECT_SQL))
