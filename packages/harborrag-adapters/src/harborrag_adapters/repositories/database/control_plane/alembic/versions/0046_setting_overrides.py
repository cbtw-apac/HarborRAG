"""Add runtime_settings: per-scope runtime config overrides resolved default -> tenant -> project -> connection.

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

_JSONB = sa.JSON().with_variant(JSONB(), "postgresql")
_TZDT = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("scope_level", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", _JSONB, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "scope_level",
            "scope_id",
            "key",
            name="pk_runtime_settings",
        ),
        sa.CheckConstraint(
            "scope_level IN ('tenant', 'project', 'connection')",
            name="ck_runtime_settings_scope_level",
        ),
    )
    op.create_index(
        "ix_runtime_settings_tenant_key",
        "runtime_settings",
        ["tenant_id", "key"],
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_settings_tenant_key", table_name="runtime_settings")
    op.drop_table("runtime_settings")
