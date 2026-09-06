"""webhook health tracking + runs.avh_object_id + audit_log indexes — v1.2.5

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-01

All additive, mirroring models.py:
- webhooks: last_success_at / last_failure_at / consecutive_failures / disabled_reason —
  per-webhook health observability (previously only reconstructable by joining
  webhook_deliveries), plus disable-after-N-consecutive-failures support.
- runs.avh_object_id: opt-in pointer to a published `.avh` context-memory document
  (`av handoff --publish`), so the WebUI run-detail view can render context notes
  without the server ever seeing them unless the repo owner explicitly publishes.
- audit_log: indexes on username and action — richer filters (actor, route family)
  land in the same release and need these to stay fast at scale.

No existing row is touched; legacy volumes get these via the startup heal path.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("webhooks", sa.Column("last_success_at", sa.DateTime(), nullable=True))
    op.add_column("webhooks", sa.Column("last_failure_at", sa.DateTime(), nullable=True))
    op.add_column(
        "webhooks",
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("webhooks", sa.Column("disabled_reason", sa.Text(), nullable=True))

    op.add_column("runs", sa.Column("avh_object_id", sa.Text(), nullable=True))

    op.create_index("ix_audit_log_username", "audit_log", ["username"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_username", table_name="audit_log")
    op.drop_column("runs", "avh_object_id")
    op.drop_column("webhooks", "disabled_reason")
    op.drop_column("webhooks", "consecutive_failures")
    op.drop_column("webhooks", "last_failure_at")
    op.drop_column("webhooks", "last_success_at")
