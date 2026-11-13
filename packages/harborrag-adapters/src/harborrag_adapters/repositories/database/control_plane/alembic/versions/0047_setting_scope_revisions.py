"""Add runtime_setting_revisions: one monotonic revision per scope, for PATCH /v1/admin/settings ETag/If-Match.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

_TZDT = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "runtime_setting_revisions",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("scope_level", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(32), nullable=False, server_default="api"),
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column("updated_at", _TZDT, nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "scope_level",
            "scope_id",
            name="pk_runtime_setting_revisions",
        ),
        sa.CheckConstraint(
            "scope_level IN ('tenant', 'project', 'connection')",
            name="ck_runtime_setting_revisions_scope_level",
        ),
    )


def downgrade() -> None:
    op.drop_table("runtime_setting_revisions")
