"""Non-superuser app role — closes the RLS-superuser gap documented in 0013 — v1.3.2

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-05

Migration 0013's own docstring flagged, plainly, that row-level security is INERT under
this repo's own default `docker-compose.yml` because `av_user` (the role Alembic itself
connects as, granted by the official `postgres` image simply because it names
`POSTGRES_USER`) is a database SUPERUSER — and Postgres unconditionally exempts
superusers from RLS, `FORCE ROW LEVEL SECURITY` included. That docstring also explained
why the fix could not be a rushed mid-migration edit: it needs "a new role, ownership
transfer, `DATABASE_URL` update, a fresh connection pool" — a careful rollout, not a
one-line change. This migration is that rollout's schema half.

**What this creates:** a second Postgres role, `av_app` — LOGIN, ordinary (no
SUPERUSER/BYPASSRLS/CREATEROLE/CREATEDB), granted exactly SELECT/INSERT/UPDATE/DELETE on
every table and USAGE/SELECT on every sequence in `public`, via `ALTER DEFAULT
PRIVILEGES` so any table a FUTURE migration adds (still applied as `av_user`, which
keeps needing DDL rights) is automatically covered too — no per-migration grant
maintenance needed from here on.

**What this does NOT do by itself:** nothing connects as `av_app` yet merely because this
migration ran — `database.py` gains an optional `AV_APP_DATABASE_URL` (this same commit)
that, when set, routes ordinary request-serving sessions through a second engine
connected as this role, while migrations and the two legitimately cross-tenant
background workers (`system_session_factory`) keep using `DATABASE_URL` (`av_user`,
still needed for DDL and for the GUC-based `app.bypass_rls` escape hatch 0013 already
documents). `docker-compose.yml` sets `AV_APP_DATABASE_URL` for this repo's own default
topology (this same commit), so the gap 0013 documented is closed there specifically —
any OTHER deployment that never sets the new env var keeps today's exact behavior
(single role, RLS inert, byte-identical), per guardrail #1: nothing new is on by default
for a deployment that doesn't opt in.

Password is a fixed literal, matching this repo's existing dev-credential posture
(`av_password` is already a hardcoded literal in `docker-compose.yml` for `av_user`) —
`CREATE ROLE ... PASSWORD` is DDL and rejects bind parameters over asyncpg's extended
query protocol exactly like `CREATE POLICY` did in 0013, so the value is inlined, not
parameterized. A real production deployment is expected to rotate it
(`ALTER ROLE av_app WITH PASSWORD '...'`) the same way it would rotate `av_password` —
this migration does not invent new secret-management machinery this repo doesn't have.

Idempotent (`IF NOT EXISTS` guard) so `upgrade` is safe to run twice, and a full
`upgrade head -> downgrade base -> upgrade head` cycle (this chain's own test coverage,
`tests/test_migrations.py`) round-trips cleanly: `downgrade()` revokes every grant THIS
database's upgrade() made. It deliberately does NOT `DROP ROLE` — found live, not
anticipated: `av_app` is a cluster-wide Postgres role, and a real cluster commonly hosts
more than one database against it (this repo's own dev machine does: `aether_vault` +
`aether_vault_test`); `DROP ROLE` fails whenever the role still holds a grant in ANY
OTHER database on the cluster, which a per-database migration cannot see or revoke. See
`downgrade()`'s own docstring below for the full finding.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_ROLE = "av_app"
# Matches the existing docker-compose.yml `av_password` literal's posture exactly: a
# fixed dev credential, not a secret-manager-issued one. See module docstring.
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

# `EXECUTE format(...)` (dynamic SQL inside a DO block) rather than a plain
# `GRANT CONNECT ON DATABASE <literal> TO ...` because the database name isn't a fixed
# constant this module controls the way the role name and password above are — reading
# it from `current_database()` at run time means this migration works unmodified against
# any database name, not just this repo's own `aether_vault` default.
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

# `ALTER DEFAULT PRIVILEGES FOR ROLE <name>` needs the MIGRATING role's own name as an
# identifier -- originally hardcoded to the literal `av_user` (the role this repo's own
# `docker-compose.yml` happens to name its Postgres superuser/migrator), which is exactly
# the same "not a fixed constant this module controls" mistake `_GRANT_CONNECT_SQL`'s own
# comment already warns against for the database name, just made here for the role name
# instead. Found live: this repo's own Windows CI runner provisions PostgreSQL fresh via
# Chocolatey with only a `postgres` superuser and no `av_user` role at all -- migrating
# there raised `UndefinedObjectError: role "av_user" does not exist`, on every single
# fixture setup, cascading into 146+ test ERRORs from one bad literal. Fixed the same way
# `_GRANT_CONNECT_SQL` already fixed the database-name version of this exact mistake:
# `EXECUTE format(...)` against `CURRENT_USER` (whichever role is ACTUALLY running this
# migration, on ANY deployment) instead of a hardcoded name. Semantically identical to the
# original intent either way -- the migrating role is always the one that creates future
# tables/sequences, so "future objects THE MIGRATOR creates" and "future objects av_user
# creates" mean the same thing on this repo's own topology, and the dynamic form is also
# now correct everywhere else.
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
    """Revokes every grant THIS database's upgrade() made — deliberately does NOT
    `DROP ROLE`. Found live, not anticipated: `av_app` is a Postgres ROLE, which is a
    CLUSTER-WIDE object, not a per-database one — a real Postgres cluster commonly hosts
    more than one database against the same role (this repo's own dev machine does:
    `aether_vault` and `aether_vault_test` on one cluster, both migrated by this same
    chain). `DROP ROLE` fails with `DependentObjectsStillExistError` whenever the role
    still holds so much as a schema-USAGE grant in ANY OTHER database on the cluster —
    which a per-database migration has no visibility into and no business touching
    (Postgres privileges are strictly per-database; a session connected to
    `aether_vault_test` cannot revoke a grant that lives in `aether_vault`). Attempting
    the DROP here would make downgrading ONE database's schema fail depending on
    unrelated state in a SIBLING database, which is exactly the kind of hidden
    cross-database coupling a migration must never have.

    Net effect: `downgrade` leaves an empty, privilege-less `av_app` role behind after
    stripping every grant in the current database. That is harmless (an unprivileged
    role that can still LOG IN but can no longer CONNECT to this database at all, let
    alone read/write anything in it) and is the same trade-off Postgres itself documents
    for `DROP OWNED BY` vs `DROP ROLE` across multiple databases: an operator who wants
    the role gone CLUSTER-WIDE, once no database still uses it, runs `DROP ROLE av_app;`
    by hand — a one-line manual step, not something a single database's automated
    migration chain can safely do on its own.
    """
    conn = op.get_bind()
    for stmt in _REVOKE_STATEMENTS:
        conn.execute(sa.text(stmt))
    conn.execute(sa.text(_REVOKE_CONNECT_SQL))
