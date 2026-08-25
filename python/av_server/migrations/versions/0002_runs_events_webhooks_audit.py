"""runs/events/webhooks/audit — autonomous-loop layer (v1.2.0)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-24

Adds the first-class Run/Experiment entity (runs + run_commits join), the resumable
event stream (events), signed webhook subscriptions (webhooks), and the audit trail
(audit_log). All additive — no existing table is touched, so legacy healed volumes
upgrade zero-touch exactly like fresh ones.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="created"),
        sa.Column("parent_run_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("config_hash", sa.String(), nullable=True),
        sa.Column("code_pointer", sa.JSON(), nullable=True),
        sa.Column("env_snapshot_id", sa.String(), nullable=True),
        sa.Column("metrics_summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
    )
    op.create_index("ix_runs_project_status", "runs", ["project_id", "status"])
    op.create_index("ix_runs_parent", "runs", ["parent_run_id"])

    op.create_table(
        "run_commits",
        sa.Column("run_id", sa.String(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("commit_hash", sa.String(), sa.ForeignKey("commits.hash"), primary_key=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True, index=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
    )
    op.create_index("ix_events_project_kind_id", "events", ["project_id", "kind", "id"])

    op.create_table(
        "webhooks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True, index=True),
        sa.Column("kinds", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True, index=True),
        sa.Column("details", sa.JSON(), nullable=True),
    )
    op.create_index("ix_audit_ts", "audit_log", ["ts"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("webhooks")
    op.drop_index("ix_events_project_kind_id", table_name="events")
    op.drop_table("events")
    op.drop_table("run_commits")
    op.drop_index("ix_runs_parent", table_name="runs")
    op.drop_index("ix_runs_project_status", table_name="runs")
    op.drop_table("runs")
