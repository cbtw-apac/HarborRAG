"""Store encrypted secret values alongside their refs.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: no prior row ever had a value to backfill -- the "secrets"
    # table existed with refs/provenance only, no backend that could persist
    # one. SqlSecretsRepository.put() always sets this on insert.
    op.add_column(
        "secrets",
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("secrets", "ciphertext")
