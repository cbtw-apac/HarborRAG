"""Add projects.version for conditional writes (ETag and If-Match).

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("version", sa.Integer(), server_default="1", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("version")
