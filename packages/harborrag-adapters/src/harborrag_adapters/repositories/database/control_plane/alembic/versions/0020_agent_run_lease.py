"""Add an executor lease to agent-run checkpoints.

``resume`` previously accepted any RUNNING checkpoint, so a second worker
could replay a step the still-live original was executing. The lease fences
that: the executing worker stamps ``lease_owner``/``lease_expires_at`` on
every persisted step, ``resume`` refuses a RUNNING run whose lease is live
(HarborConflictError) and claims one whose lease lapsed (the crash case).
Terminal statuses clear both columns.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_TABLE = "agent_runs"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("lease_owner", sa.Text(), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_owner")
