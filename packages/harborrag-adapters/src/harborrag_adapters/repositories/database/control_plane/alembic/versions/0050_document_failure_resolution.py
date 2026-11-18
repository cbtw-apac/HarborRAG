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
    # Backfill: a failure row for a version that is already ACTIVE is already
    # resolved -- it predates this column and would otherwise sit at NULL
    # forever, since `publish`'s replay branch for an already-ACTIVE version
    # returns before it ever reaches the resolution update. A failure on a
    # version that never published stays NULL, same as a fresh unresolved row.
    op.execute(
        """
        UPDATE document_failures
        SET resolved_at = (
            SELECT document_versions.activated_at
            FROM document_versions
            WHERE document_versions.document_version_id = document_failures.document_version_id
              AND document_versions.status = 'ACTIVE'
        )
        WHERE resolved_at IS NULL
          AND EXISTS (
            SELECT 1
            FROM document_versions
            WHERE document_versions.document_version_id = document_failures.document_version_id
              AND document_versions.status = 'ACTIVE'
          )
        """
    )


def downgrade() -> None:
    op.drop_column("document_failures", "resolved_at")
