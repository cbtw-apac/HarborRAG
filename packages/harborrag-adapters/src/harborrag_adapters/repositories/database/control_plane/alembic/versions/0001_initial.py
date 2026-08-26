"""0001: consolidated control-plane and ingestion schema baseline.

Frozen DDL for the first release. Future schema changes must add a new
revision instead of editing this baseline.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from harborrag_adapters.repositories.database.control_plane.alembic.baseline import (
    control_plane,
    ingestion,
)

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the full initial control-plane and ingestion schema."""

    control_plane.create_tables()
    ingestion.create_tables()


def downgrade() -> None:
    """Drop the complete baseline in reverse dependency order."""

    ingestion.drop_tables()
    control_plane.drop_tables()
