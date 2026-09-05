"""Per-tenant CAS storage — objects/trees primary key widening — v1.3.2

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-04

Widens `objects.hash` (bare PK) to `(tenant_id, hash)` and `trees.(tree_hash, path_name)`
to `(tenant_id, tree_hash, path_name)` — the one schema change every other tenant-scoped
table (migrations 0012/0013) deliberately did NOT apply to these two, because they are
pure content-addressed, GLOBALLY deduplicated stores: two different projects committing
byte-identical content have always shared one row and one physical file.

**Scope of this migration, stated precisely: schema only.** Every `objects`/`trees` row
backfills to `DEFAULT_TENANT_ID` — same as every other table — so the widened composite
key degrades to exactly the old bare-`hash`/`(tree_hash, path_name)` semantics today:
one tenant, one dedup domain, byte-identical to pre-v1.3.2. `server.py`'s query/insert
sites were updated to always include a `tenant_id` predicate (correctness for the
widened PK, not conditional on any flag) — see those call sites directly.

**What this migration deliberately does NOT ship, so nothing here implies it does:** a
genuine second dedup domain (a real `AV_CAS_ISOLATION=isolated` mode giving each tenant
physically separate object storage and a separate Bloom filter) is NOT implemented by
this migration or by the current `storage.py`/`redis_cache.py`. Building that requires
its own careful design specifically to avoid a real data-loss bug identified during
planning: a global existence check (Bloom filter or DB row) would tell a second tenant
"already have it" and skip their upload, leaving their commit referencing bytes that
were never actually written under a genuinely separate per-tenant store. The schema here
is a correct, safe PREREQUISITE for that future work (and is exercised today under the
one real tenant every unconfigured deployment has), not the feature itself — do not read
this migration's presence as isolation being available to turn on.
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
    offline = context.is_offline_mode()  # see migration 0012's own note on why this
                                          # matters for a live-rowcount-driven loop

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

    # Widen the primary key. Postgres requires dropping the old PK constraint first —
    # the constraint NAME follows this project's existing convention
    # (`<table>_pkey`, Postgres's own default naming, confirmed against the live schema
    # rather than assumed) before adding the new composite one.
    op.drop_constraint("objects_pkey", "objects", type_="primary")
    op.create_primary_key("objects_pkey", "objects", ["tenant_id", "hash"])
    op.create_foreign_key("fk_objects_tenant_id", "objects", "tenants", ["tenant_id"], ["id"])

    op.drop_constraint("trees_pkey", "trees", type_="primary")
    op.create_primary_key("trees_pkey", "trees", ["tenant_id", "tree_hash", "path_name"])
    op.create_foreign_key("fk_trees_tenant_id", "trees", "tenants", ["tenant_id"], ["id"])

    # Non-PK lookup indexes: object/tree existence and GC's mark phase both query by
    # hash alone within a tenant in some paths (e.g. a legacy single-tenant lookup); the
    # composite PK's leading column IS tenant_id, so a bare `WHERE hash = :h` (no
    # tenant_id predicate) cannot use the PK index efficiently. These support that shape
    # without requiring every call site to add a tenant_id predicate on day one.
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
