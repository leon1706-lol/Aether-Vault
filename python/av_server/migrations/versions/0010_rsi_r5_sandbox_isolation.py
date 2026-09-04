"""RSI R5 — sandbox jobs, tool manifests, action logs — v1.3.1

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-04

Additive: three new tables, no existing table touched. See
development/architecture.md's Sandbox Execution Contract section.

These three tables are server-side INDEX/AUDIT records, not the sandbox executor's own
state — a driver's live job state lives where the driver itself can actually re-query it
(a container, a Pod, a Slurm job — see `python/av_cli/sandbox/base.py`'s module
docstring for why). `sandbox_jobs` lets `av sandbox queue` list jobs across drivers/
machines without each driver needing its own listing capability (Slurm's `squeue`
already lists that user's jobs, but a `local` job on a laptop has no such listing at
all); `tool_manifests` and `action_logs` follow the same content-addressed +
version-history pattern as every other RSI artifact table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sandbox_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("improver_id", sa.String(), nullable=True, index=True),
        sa.Column("driver", sa.String(), nullable=False),  # local | docker | kubernetes | slurm
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("command", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    op.create_table(
        "tool_manifests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("improver_id", sa.String(), nullable=False, index=True),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "action_logs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("run_id", sa.String(), nullable=True, index=True),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("action_logs")
    op.drop_table("tool_manifests")
    op.drop_table("sandbox_jobs")
