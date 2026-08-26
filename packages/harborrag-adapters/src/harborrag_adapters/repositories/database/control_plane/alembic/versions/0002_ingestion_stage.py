"""Compatibility marker for the ingestion-stage revision.

Revision ID: 0002
Revises: 0001

The revision's DDL is included in the consolidated 0001 baseline.  Keeping
the revision ID lets databases created before the consolidation upgrade to
newer revisions without depending on stale bytecode.
"""

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Schema changes are already represented by the frozen baseline."""


def downgrade() -> None:
    """Schema changes are removed by the frozen baseline."""
