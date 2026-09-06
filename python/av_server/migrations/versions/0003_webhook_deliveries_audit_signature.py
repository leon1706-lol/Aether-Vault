"""webhook deliveries + audit outcome + commit signatures — v1.2.2

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-25

All additive, mirroring models.py:
- webhook_deliveries: per-attempt webhook fan-out ledger with retry/dead-letter columns.
- audit_log.status_code: HTTP outcome per mutation (audit depth).
- commits.signature: ed25519 signature blob (canonical-form signing) so signatures
  survive clone/pull round trips.

No existing row is touched; legacy volumes get the new columns via the startup heal
path (_LEGACY_COLUMNS) instead of a replay.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("webhook_id", sa.String(), sa.ForeignKey("webhooks.id"), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("event_kind", sa.String(), nullable=True),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )
    op.create_index("ix_webhook_deliveries_webhook_id", "webhook_deliveries", ["webhook_id"])
    op.create_index("ix_webhook_deliveries_event_id", "webhook_deliveries", ["event_id"])
    op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])
    op.create_index("ix_webhook_deliveries_next_retry_at", "webhook_deliveries", ["next_retry_at"])

    op.add_column("audit_log", sa.Column("status_code", sa.Integer(), nullable=True))
    op.add_column("commits", sa.Column("signature", sa.Text(), nullable=True))
    op.add_column("commits", sa.Column("env_snapshot_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("commits", "env_snapshot_id")
    op.drop_column("commits", "signature")
    op.drop_column("audit_log", "status_code")
    op.drop_index("ix_webhook_deliveries_next_retry_at", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_status", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_event_id", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_webhook_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
