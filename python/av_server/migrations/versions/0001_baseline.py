"""baseline — full av_server schema as of v1.1.x (objects, trees, commits, refs)

Revision ID: 0001
Revises:
Create Date: 2026-08-22

Hand-written to match python/av_server/models.py exactly at the point migrations were
adopted (v1.1.1 cycle). Databases created by the old create_all startup predate this
revision and are handled zero-touch by database.py's legacy-detection path: it heals
the two columns that postdate them (commits.extra_parents, trees.chunks) and stamps
this revision as applied, so only future revisions ever execute on them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "objects",
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("hash"),
    )
    op.create_table(
        "trees",
        sa.Column("tree_hash", sa.String(), nullable=False),
        sa.Column("path_name", sa.String(), nullable=False),
        sa.Column("child_tree_hash", sa.String(), nullable=True),
        # No FK on object_hash by design: layer-split safetensors / CDC-chunked
        # checkpoints never upload a whole-file blob, so this hash intentionally has
        # no guaranteed row in `objects` (content identity only).
        sa.Column("object_hash", sa.String(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("layers", sa.JSON(), nullable=True),
        sa.Column("chunks", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("tree_hash", "path_name"),
    )
    op.create_table(
        "commits",
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        # No FK on parent_hash by design: pushes can be shallow/out-of-order (offline
        # pending queue, clones) and must not fail on a missing parent row.
        sa.Column("parent_hash", sa.String(), nullable=True),
        sa.Column("extra_parents", sa.String(), nullable=True),
        sa.Column("root_tree_hash", sa.String(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("project_name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("hash"),
    )
    op.create_index(op.f("ix_commits_parent_hash"), "commits", ["parent_hash"])
    op.create_index(op.f("ix_commits_project_id"), "commits", ["project_id"])
    op.create_table(
        "refs",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("commit_hash", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["commit_hash"], ["commits.hash"]),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("refs")
    op.drop_index(op.f("ix_commits_project_id"), table_name="commits")
    op.drop_index(op.f("ix_commits_parent_hash"), table_name="commits")
    op.drop_table("commits")
    op.drop_table("trees")
    op.drop_table("objects")
