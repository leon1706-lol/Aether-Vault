"""RSI R2 — task/eval registry, integrity signals — v1.3.1

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-04

Additive: one new nullable column on `runs` (`integrity_signals`) plus four new tables.
`eval_suites`/`eval_adapters` index a CAS object for their content; `eval_results`
stores its score/detail payload inline (JSON), since a score is small, per-run, and
queried far more often than any manifest. No foreign keys on `*_id` columns, same
shallow/out-of-order-write rationale as prior RSI migrations.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("integrity_signals", sa.JSON(), nullable=True))

    op.create_table(
        "eval_suites",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("frozen", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("blind", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    op.create_table(
        "eval_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("suite_id", sa.String(), nullable=False, index=True),
        sa.Column("run_id", sa.String(), nullable=True, index=True),
        sa.Column("score", sa.JSON(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("revealed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("scored_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "eval_adapters",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("command", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("difficulty", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="proposed"),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("tasks")
    op.drop_table("eval_adapters")
    op.drop_table("eval_results")
    op.drop_table("eval_suites")
    op.drop_column("runs", "integrity_signals")
