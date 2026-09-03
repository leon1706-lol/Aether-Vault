"""runs.policy_outcome — v1.3.0

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-02

Additive: `runs.policy_outcome` (JSON, nullable) records the most recent av promote/merge
policy decision made for a run's active commit — {"decision": "allow"|"deny", "rule":
str|None, "at": ISO-8601}. Reported via POST /api/runs/{id}/policy-outcome right after
enforce_policy()/promote() decides. Null until the first decision for a given run — no
existing row is touched, and legacy healed volumes upgrade zero-touch exactly like fresh
ones (see python/av_server/database.py::_LEGACY_COLUMNS).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("policy_outcome", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "policy_outcome")
