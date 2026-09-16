"""Soft-delete providers instead of removing the row (ML4-P2).

A ``routing_rules`` row can reference a provider's id via a DB foreign key
(0001 baseline); a hard delete of a referenced provider either violates that
constraint or leaves a dangling reference depending on the backend. Deleting
now sets ``deleted_at`` and keeps the row so the reference stays valid;
``list``/``get`` hide deleted providers as if they were gone.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "providers",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("providers", "deleted_at")
