"""RSI R3 — experiment plans, budget accounts, auto-stop — v1.3.1

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-04

Additive: three new nullable columns on `runs` (`plan_id`, `budget_id`, `stop_reason`)
plus two new tables. No existing row is touched. See
development/architecture.md's Research Control Contract section.

`plans` indexes a CAS object (hypotheses/ablations/budget/stop-rules), same pattern as
every other RSI artifact table. `budgets` stores its counters inline (JSON would hide them
from SQL aggregation, which `av budget show`'s cross-lineage rollup needs).

No foreign keys on `*_id` columns — same shallow/out-of-order-write rationale as every
prior RSI migration's tables.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("plan_id", sa.String(), nullable=True))
    op.add_column("runs", sa.Column("budget_id", sa.String(), nullable=True))
    op.add_column("runs", sa.Column("stop_reason", sa.String(), nullable=True))
    op.create_index("ix_runs_plan_id", "runs", ["plan_id"])
    op.create_index("ix_runs_budget_id", "runs", ["budget_id"])

    op.create_table(
        "plans",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "budgets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("scope", sa.String(), nullable=False),  # "run" | "lineage"
        sa.Column("scope_ref", sa.String(), nullable=False, index=True),
        sa.Column("compute_seconds_limit", sa.Float(), nullable=True),
        sa.Column("storage_bytes_limit", sa.BigInteger(), nullable=True),
        sa.Column("step_limit", sa.Integer(), nullable=True),
        sa.Column("compute_seconds_used", sa.Float(), nullable=False, server_default="0"),
        sa.Column("storage_bytes_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("steps_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("budgets")
    op.drop_table("plans")
    op.drop_index("ix_runs_budget_id", table_name="runs")
    op.drop_index("ix_runs_plan_id", table_name="runs")
    op.drop_column("runs", "stop_reason")
    op.drop_column("runs", "budget_id")
    op.drop_column("runs", "plan_id")
