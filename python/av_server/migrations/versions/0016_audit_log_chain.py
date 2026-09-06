"""Audit log hash-chaining + optional signing — v1.3.3 (WP-32)

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-05

`audit_log` gains two columns: `chain_hash` (NOT NULL after backfill) folds in the
previous row's chain_hash plus this row's own content, chaining purely by `id`'s
monotonic order so any tampered/deleted row breaks every hash after it; `signature`
(nullable, stays NULL unless `AV_AUDIT_SIGNING_KEY_PATH` is configured) is an ed25519
signature over `chain_hash` adding non-repudiation for exports.

Every existing row is backfilled with a real chain_hash by walking the table in `id`
order -- this cannot prove a row wasn't tampered with before the migration ran, only
that every row from this point forward is verifiable. Concurrent audit writes are
serialized around a `pg_advisory_xact_lock` in the runtime listener
(`database.py::_chain_audit_log`) to prevent two requests forking the chain from the
same "last chain_hash" read.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from alembic import context

    op.add_column("audit_log", sa.Column("chain_hash", sa.String(), nullable=True))
    op.add_column("audit_log", sa.Column("signature", sa.String(), nullable=True))

    # Live backfill only -- offline (--sql) mode has no real rows to walk (see 0012's
    # note on is_offline_mode()).
    if not context.is_offline_mode():
        from av_server.audit_chain import compute_chain_hash

        conn = op.get_bind()
        rows = conn.execute(
            sa.text(
                "SELECT id, ts, username, action, project_id, status_code, details "
                "FROM audit_log ORDER BY id ASC"
            )
        ).fetchall()

        prev_hash = None
        updates = []
        for row in rows:
            chain_hash = compute_chain_hash(
                prev_hash, row.ts, row.username, row.action, row.project_id,
                row.status_code, row.details,
            )
            updates.append({"row_id": row.id, "chain_hash": chain_hash})
            prev_hash = chain_hash

        if updates:
            conn.execute(
                sa.text("UPDATE audit_log SET chain_hash = :chain_hash WHERE id = :row_id"),
                updates,
            )

    op.alter_column("audit_log", "chain_hash", nullable=False)


def downgrade() -> None:
    op.drop_column("audit_log", "signature")
    op.drop_column("audit_log", "chain_hash")
