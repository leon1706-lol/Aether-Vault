"""Enterprise identity & tenancy — v1.3.2: tenants, projects, users, roles, tokens, SSO,
sessions.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-04

Purely additive: eleven NEW tables, no existing table touched (tenant_id lands on
existing tables in migrations 0012/0013, split out to avoid one long lock across
add-column + backfill + NOT-NULL + RLS-enable on tables the size `audit_log`/`events`
can reach in a live deployment).

Seeds a single well-known DEFAULT tenant (`models.py::DEFAULT_TENANT_ID`, the same fixed
UUID literal migration 0013's RLS policies fall back to) and six built-in roles whose
`permissions` are expressed in the EXISTING v1.3.1 scope vocabulary
(`server.py::require_scope()`'s scope strings) — a role binding is a different, DB-backed
way to arrive at the same `scopes` list `_scopes_for_identity()` already resolves for an
`.env`-based token, not a parallel permission system. `owner`'s `["*"]` is the exact
wildcard `_scopes_for_identity()` already returns for the `AV_API_TOKEN` shared secret
and for any token that declares no explicit scopes — this is what keeps the new RBAC
surface purely additive: nothing that could already reach a route loses access.

`projects` is backfilled from `SELECT DISTINCT project_id, project_name FROM commits` —
before this migration, "which project_ids exist" was purely virtual (`GET /api/projects`
was a live `GROUP BY` over `commits`, see server.py::list_projects); this is the first
migration to make it a real, ownable row. Every backfilled project is owned by the
default tenant, matching every pre-existing commit's implicit single-tenant world.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# id, name, permissions (the existing require_scope() vocabulary), builtin
_BUILTIN_ROLES = [
    ("role-owner", "owner", ["*"]),
    ("role-admin", "admin", ["admin", "improver:write", "policy:write", "eval:write",
                              "review", "scorer", "token:write", "user:write", "scim"]),
    ("role-maintainer", "maintainer", ["improver:write", "policy:write", "review"]),
    ("role-trainer", "trainer", ["improver:write", "scorer"]),
    ("role-reviewer", "reviewer", ["review"]),
    ("role-reader", "reader", ["read"]),
]


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)

    op.create_table(
        "projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_projects_tenant", "projects", ["tenant_id"])

    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("source", sa.String(), nullable=False, server_default="local"),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_users_tenant", "users", ["tenant_id"])
    op.create_index("ix_users_external_id", "users", ["external_id"])
    op.create_index("ix_users_tenant_username", "users", ["tenant_id", "username"], unique=True)
    op.create_index("ix_users_tenant_email", "users", ["tenant_id", "email"], unique=True)

    op.create_table(
        "user_identities",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider_id", sa.String(), nullable=False),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_user_identities_user", "user_identities", ["user_id"])
    op.create_index("ix_user_identities_provider", "user_identities", ["provider_id"])
    op.create_index("ix_user_identities_provider_subject", "user_identities",
                     ["provider_id", "subject"], unique=True)

    op.create_table(
        "groups",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="local"),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_groups_tenant", "groups", ["tenant_id"])
    op.create_index("ix_groups_external_id", "groups", ["external_id"])

    op.create_table(
        "group_members",
        sa.Column("group_id", sa.String(), sa.ForeignKey("groups.id"), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_roles_tenant", "roles", ["tenant_id"])
    op.create_index("ix_roles_tenant_name", "roles", ["tenant_id", "name"], unique=True)

    op.create_table(
        "role_bindings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("role_id", sa.String(), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False, server_default="tenant"),
        sa.Column("scope_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_role_bindings_subject", "role_bindings",
                     ["tenant_id", "subject_type", "subject_id"])

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_api_tokens_tenant", "api_tokens", ["tenant_id"])
    op.create_index("ix_api_tokens_hash", "api_tokens", ["token_hash"], unique=True)

    op.create_table(
        "sso_providers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_sso_providers_tenant", "sso_providers", ["tenant_id"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("refresh_hash", sa.String(), nullable=True),
        sa.Column("issued_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("ip", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
    )
    op.create_index("ix_sessions_user", "sessions", ["user_id"])
    op.create_index("ix_sessions_tenant", "sessions", ["tenant_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_refresh_hash", "sessions", ["refresh_hash"], unique=True)

    # --- seed data ---------------------------------------------------------------
    now = sa.func.now()
    tenants_t = sa.table(
        "tenants", sa.column("id", sa.String), sa.column("slug", sa.String),
        sa.column("name", sa.String), sa.column("status", sa.String),
    )
    op.bulk_insert(tenants_t, [
        {"id": DEFAULT_TENANT_ID, "slug": "default", "name": "Default Tenant", "status": "active"},
    ])

    # Seeded via a raw parameterized INSERT with an explicit CAST(:permissions AS JSON),
    # not op.bulk_insert — two failure modes were found and rejected in turn while
    # writing this migration:
    #   1. sa.Column("permissions", sa.JSON()) in the bulk_insert helper table: offline
    #      (--sql) rendering has no literal-value renderer for JSON at all (sa.JSON
    #      defines none) — fails test_chain_renders_complete_postgres_ddl_offline.
    #   2. Declaring that helper column sa.String instead fixes offline rendering (an
    #      untyped SQL string literal implicitly coerces to a JSON column), but then
    #      breaks ONLINE execution: asyncpg's extended query protocol binds a VARCHAR
    #      parameter with an explicit type, and Postgres refuses to insert a bound
    #      VARCHAR into a JSON column with no cast — DatatypeMismatchError, found by
    #      actually round-tripping this migration live (downgrade + re-upgrade) against
    #      the real Docker Postgres, not by the offline-only test suite.
    # An explicit `CAST(:permissions AS JSON)` in raw SQL is the one form that is
    # correct in BOTH modes: offline literal-binds mode inlines the bound value as a
    # literal already wrapped in the same CAST; online mode binds it as text and lets
    # the cast do the conversion server-side either way.
    import json as _json

    conn = op.get_bind()
    for rid, name, perms in _BUILTIN_ROLES:
        conn.execute(sa.text("""
            INSERT INTO roles (id, tenant_id, name, description, permissions, builtin)
            VALUES (:id, NULL, :name, :description, CAST(:permissions AS JSON), TRUE)
        """), {
            "id": rid, "name": name, "description": f"Built-in {name} role",
            "permissions": _json.dumps(perms),
        })

    # Backfill projects from every project_id ever pushed, owned by the default tenant.
    conn.execute(sa.text("""
        INSERT INTO projects (id, tenant_id, name, created_at)
        SELECT DISTINCT ON (project_id) project_id, :tenant_id,
               COALESCE(project_name, project_id), now()
        FROM commits
        ORDER BY project_id
        ON CONFLICT (id) DO NOTHING
    """), {"tenant_id": DEFAULT_TENANT_ID})


def downgrade() -> None:
    op.drop_table("sessions")
    op.drop_table("sso_providers")
    op.drop_table("api_tokens")
    op.drop_table("role_bindings")
    op.drop_table("roles")
    op.drop_table("group_members")
    op.drop_table("groups")
    op.drop_table("user_identities")
    op.drop_table("users")
    op.drop_table("projects")
    op.drop_table("tenants")
