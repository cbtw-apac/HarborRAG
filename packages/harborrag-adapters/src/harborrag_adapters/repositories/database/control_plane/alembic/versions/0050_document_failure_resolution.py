"""Add document_failures.resolved_at so recovered documents stop listing as failing.

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

_TZDT = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("document_failures", sa.Column("resolved_at", _TZDT, nullable=True))


def downgrade() -> None:
    op.drop_column("document_failures", "resolved_at")
