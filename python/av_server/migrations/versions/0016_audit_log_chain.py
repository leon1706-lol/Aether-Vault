"""Audit log hash-chaining + optional signing — v1.3.3 (WP-32)

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-05

Makes SECURITY.md's audit-logging claim true. `audit_log` gains two columns:

- `chain_hash` (NOT NULL after backfill) — `audit_chain.compute_chain_hash()` folds in
  the PREVIOUS row's chain_hash plus this row's own content, so tampering or deleting
  any row breaks every chain_hash computed after it. Unlike `policy_packs`'
  `prev_id`+`chain_hash` (a client-chosen previous pack, since packs can in principle be
  published out of strict sequence), audit rows have no `prev_id` column at all — they
  chain purely by `id`'s own natural monotonic order (an autoincrement primary key), so
  verification is always an unambiguous `ORDER BY id ASC` walk.
- `signature` (nullable, stays NULL unless `AV_AUDIT_SIGNING_KEY_PATH` is configured) —
  an ed25519 signature over `chain_hash`, from `audit_signing.py`'s server-wide
  keypair (deliberately separate from `av_cli/signing.py`'s per-repo commit-signing
  keys). Optional and additive: chain-hashing alone is already tamper-evident against
  in-place row edits; signing adds non-repudiation for an export handed to a party with
  no direct database access.

**Historical backfill, and its honest limit.** Every EXISTING row gets a real
chain_hash computed by walking the table in `id` order — this migration does not leave
pre-existing rows with a null/placeholder value. What this does NOT and CANNOT prove:
that a pre-existing row wasn't already tampered with before this migration ever ran — no
retroactive backfill can prove that, for any tamper-evidence scheme. What it DOES
establish, honestly: a complete, gap-free chain across the WHOLE table from this point
forward, old rows included — deleting or editing ANY row (old or new) after this
migration runs breaks verification.

**Concurrency, solved with a Postgres advisory transaction lock, not left implicit.**
Two concurrent requests both auditing at once could otherwise both read the same "last
chain_hash" and both compute a hash chained from it — a real fork, not a hypothetical
one, verified by reasoning through this exact scenario before writing the runtime
listener (`database.py`'s `_chain_audit_log`). That listener acquires
`pg_advisory_xact_lock` (a session/transaction-scoped lock, auto-released at commit or
rollback) around the read-then-chain-then-insert sequence, serializing ONLY that narrow
section across concurrent transactions — everything else in each transaction proceeds
independently. This migration's own backfill needs no such lock (single-writer, DDL
context).
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

    # Live backfill only -- offline (--sql) mode has no real rows to walk, and a
    # live-rowcount-driven loop here would render nothing meaningful to a --sql dump
    # (the exact class of bug migration 0012 already hit once: a live loop offline
    # either hangs against MockConnection or silently no-ops depending on how it's
    # written -- gating on is_offline_mode() is the fix established there).
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
