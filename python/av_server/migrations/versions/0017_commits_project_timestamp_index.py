"""Composite (project_id, timestamp DESC) index on commits — V1.6.0 (WS5.7)

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-12

`list_commits`'s primary query is `WHERE project_id = ... ORDER BY timestamp DESC LIMIT
... OFFSET ...` (the Web UI dashboard's paginated commit list, and `include_layers=true`'s
per-page tree resolution rides on the same page). The existing lone single-column
`project_id` index lets Postgres filter but still forces a separate sort over every
matching row before LIMIT/OFFSET can apply on a project with a large commit history. This
composite index bakes the query's own DESC ordering directly into the index, so the same
scan can satisfy both the filter and the ordering.

Purely additive (`CREATE INDEX`, no column/table changes) — safe on a live database, no
backfill needed, and `downgrade()` is a clean drop.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_commits_project_timestamp", "commits",
        ["project_id", sa.desc("timestamp")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_commits_project_timestamp", table_name="commits")
