"""Add setting_overrides and setting_revisions: runtime settings and their revision log.

Revision ID: 0046
Revises: 0045

Phase: expand
Reversible: yes; the downgrade drops both tables and the stored overrides with them
Touches: setting_overrides, setting_revisions (new, empty)
Locks: none
Deploy: before the application release
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
        "setting_overrides",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("level", sa.String(300), nullable=False),
        sa.Column("setting_key", sa.String(128), nullable=False),
        sa.Column("value_json", _JSONB, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
        sa.Column("updated_by", sa.String(256), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "level", "setting_key", name="pk_setting_overrides"),
    )
    op.create_table(
        "setting_revisions",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("changes_json", _JSONB, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "revision", name="pk_setting_revisions"),
        sa.CheckConstraint("source IN ('seed','import','api')", name="ck_setting_revisions_source"),
        sa.CheckConstraint("revision >= 1", name="ck_setting_revisions_revision"),
    )


def downgrade() -> None:
    op.drop_table("setting_revisions")
    op.drop_table("setting_overrides")
