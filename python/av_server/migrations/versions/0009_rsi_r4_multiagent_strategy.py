"""RSI R4 — causal graphs, strategy memory, lessons, reviews, critiques, blackboard — v1.3.1

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-04

Additive: one new nullable column on `runs` (`lessons_id`) plus six new tables. No
existing row is touched. See development/architecture.md's Multi-Agent & Strategy Memory
Contract section.

`lessons` follows the same content-addressed + `/latest`-by-created_at pattern as
`policy_packs` (migration 0006) — a versioned "what we believe now" document, without the
hash-chain (lessons revise freely; they aren't a tamper-evident policy log). `reviews` and
`critiques` both key on `change_sets.id` (migration 0006) — no FK, same shallow-write
rationale as every prior RSI migration.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("lessons_id", sa.String(), nullable=True))
    op.create_index("ix_runs_lessons_id", "runs", ["lessons_id"])

    op.create_table(
        "causal_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("cause_type", sa.String(), nullable=False),  # "change_set" | "commit"
        sa.Column("cause_ref", sa.String(), nullable=False, index=True),
        sa.Column("effect_metric", sa.String(), nullable=False),
        sa.Column("effect_delta", sa.Float(), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "strategy_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("technique", sa.String(), nullable=False),
        sa.Column("hyperparameters", sa.JSON(), nullable=True),
        sa.Column("data_mix", sa.JSON(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),  # worked | failed | inconclusive
        sa.Column("run_ids", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "lessons",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("object_id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "reviews",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        # target_type/target_id: a review can approve either a change SET (pre-apply) or
        # an improver VERSION (pre-promote) — `av improver promote`'s require_review gate
        # checks reviews against the CANDIDATE improver id directly, not a change set,
        # since one improver version can be the eventual target of promotion regardless
        # of which change set produced it.
        sa.Column("target_type", sa.String(), nullable=False),  # "change_set" | "improver"
        sa.Column("target_id", sa.String(), nullable=False, index=True),
        sa.Column("reviewer", sa.String(), nullable=True),
        sa.Column("decision", sa.String(), nullable=False),  # approve | reject
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
    )

    op.create_table(
        "critiques",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        # Same target_type/target_id generalization as `reviews` above — a critique can
        # be raised against a change set OR an improver version, and `av improver
        # promote`'s gate checks unresolved/un-waived critiques against the CANDIDATE
        # improver id directly.
        sa.Column("target_type", sa.String(), nullable=False),  # "change_set" | "improver"
        sa.Column("target_id", sa.String(), nullable=False, index=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("objection", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),  # open|resolved|waived
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    op.create_table(
        "blackboard_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, index=True),
        sa.Column("claim", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),  # open|resolved
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("blackboard_entries")
    op.drop_table("critiques")
    op.drop_table("reviews")
    op.drop_table("lessons")
    op.drop_table("strategy_entries")
    op.drop_table("causal_links")
    op.drop_index("ix_runs_lessons_id", table_name="runs")
    op.drop_column("runs", "lessons_id")
