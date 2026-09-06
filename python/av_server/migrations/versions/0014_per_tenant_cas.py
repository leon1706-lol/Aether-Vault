"""Per-tenant CAS storage — objects/trees primary key widening — v1.3.2

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-04

Widens `objects.hash` (bare PK) to `(tenant_id, hash)` and `trees.(tree_hash, path_name)`
to `(tenant_id, tree_hash, path_name)` — the one schema change other tenant-scoped
migrations deliberately did NOT apply to these two, since they are pure
content-addressed, globally deduplicated stores.

Schema only: every row backfills to `DEFAULT_TENANT_ID`, so behavior today is
byte-identical to pre-v1.3.2 (one tenant, one dedup domain). A genuine second dedup
domain (per-tenant physical storage) is NOT implemented here or in `storage.py` --
building that needs its own design to avoid a data-loss bug: a global existence check
would tell a second tenant "already have it" and skip an upload that was never actually
written under a truly separate store. This migration is a safe prerequisite only.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
_BATCH_SIZE = 5000


def upgrade() -> None:
    conn = op.get_bind()
    offline = context.is_offline_mode()  # see migration 0012 re: offline rowcount

    op.add_column("objects", sa.Column("tenant_id", sa.String(), nullable=True))
    op.add_column("trees", sa.Column("tenant_id", sa.String(), nullable=True))

    for table, pk in (("objects", "hash"), ("trees", "tree_hash")):
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

    op.alter_column("objects", "tenant_id", nullable=False)
    op.alter_column("trees", "tenant_id", nullable=False)

    # Widen the primary key; Postgres requires dropping the old PK constraint first.
    op.drop_constraint("objects_pkey", "objects", type_="primary")
    op.create_primary_key("objects_pkey", "objects", ["tenant_id", "hash"])
    op.create_foreign_key("fk_objects_tenant_id", "objects", "tenants", ["tenant_id"], ["id"])

    op.drop_constraint("trees_pkey", "trees", type_="primary")
    op.create_primary_key("trees_pkey", "trees", ["tenant_id", "tree_hash", "path_name"])
    op.create_foreign_key("fk_trees_tenant_id", "trees", "tenants", ["tenant_id"], ["id"])

    # Non-PK lookup indexes: the composite PK's leading column is tenant_id, so a bare
    # `WHERE hash = :h` can't use the PK index efficiently without these.
    op.create_index("ix_objects_hash", "objects", ["hash"])
    op.create_index("ix_trees_tree_hash", "trees", ["tree_hash"])


def downgrade() -> None:
    op.drop_index("ix_trees_tree_hash", table_name="trees")
    op.drop_index("ix_objects_hash", table_name="objects")

    op.drop_constraint("fk_trees_tenant_id", "trees", type_="foreignkey")
    op.drop_constraint("trees_pkey", "trees", type_="primary")
    op.create_primary_key("trees_pkey", "trees", ["tree_hash", "path_name"])

    op.drop_constraint("fk_objects_tenant_id", "objects", type_="foreignkey")
    op.drop_constraint("objects_pkey", "objects", type_="primary")
    op.create_primary_key("objects_pkey", "objects", ["hash"])

    op.drop_column("trees", "tenant_id")
    op.drop_column("objects", "tenant_id")
