"""Allocate job-event sequence numbers atomically.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("event_sequence", sa.Integer(), server_default="0", nullable=False),
    )
    op.execute(
        sa.text(
            "UPDATE jobs SET event_sequence = COALESCE(("
            "SELECT MAX(job_events.seq) FROM job_events "
            "WHERE job_events.job_id = jobs.id), 0)"
        )
    )


def downgrade() -> None:
    op.drop_column("jobs", "event_sequence")
