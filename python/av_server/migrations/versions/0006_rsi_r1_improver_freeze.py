"""RSI R1 — improver versioning, change sets, policy packs, canaries, freeze — v1.3.1

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-03

Additive: two new nullable columns on `runs` (`kind`, `improver_id`) plus five new
tables. `runs.kind` defaults server-side to "train" via `server_default`, so a healed
legacy volume never has a NULL kind. Every new artifact table stores its actual content
as a CAS object -- these rows are lightweight index/lineage records over that content,
same pattern as `runs.env_snapshot_id`. Deliberately NO foreign keys on any `*_id`
column, matching `commits.parent_hash`'s existing shallow/out-of-order-write convention.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("kind", sa.String(), nullable=False, server_default="train"))
    op.add_column("runs", sa.Column("improver_id", sa.String(), nullable=True))
    op.create_index("ix_runs_improver_id", "runs", ["improver_id"])

    op.create_table(
        "improver_versions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("manifest_object_id", sa.String(), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=True, index=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "change_sets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("improver_id", sa.String(), nullable=True, index=True),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="proposed"),
        sa.Column("risk", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )
    op.create_index("ix_change_sets_project_status", "change_sets", ["project_id", "status"])

    op.create_table(
        "policy_packs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("prev_id", sa.String(), nullable=True),
        sa.Column("chain_hash", sa.String(), nullable=False),
        sa.Column("published_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_policy_packs_project_created", "policy_packs", ["project_id", "created_at"])

    op.create_table(
        "canary_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("improver_id", sa.String(), nullable=False, index=True),
        sa.Column("suite_object_id", sa.String(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "project_freeze",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("frozen", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("frozen_by", sa.String(), nullable=True),
        sa.Column("frozen_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("project_freeze")
    op.drop_table("canary_results")
    op.drop_index("ix_policy_packs_project_created", table_name="policy_packs")
    op.drop_table("policy_packs")
    op.drop_index("ix_change_sets_project_status", table_name="change_sets")
    op.drop_table("change_sets")
    op.drop_table("improver_versions")
    op.drop_index("ix_runs_improver_id", table_name="runs")
    op.drop_column("runs", "improver_id")
    op.drop_column("runs", "kind")
