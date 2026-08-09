"""Persist tenant identity on control-plane aggregates.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_TABLES = (
    "projects",
    "sources",
    "jobs",
    "activity",
    "providers",
    "workspace_settings",
    "members",
)


def upgrade() -> None:
    for table_name in _TABLES:
        op.add_column(
            table_name,
            sa.Column(
                "tenant_id",
                sa.String(length=128),
                server_default="DEFAULT",
                nullable=False,
            ),
        )
        op.create_index(f"ix_{table_name}_tenant_id", table_name, ["tenant_id"])


def downgrade() -> None:
    for table_name in reversed(_TABLES):
        op.drop_index(f"ix_{table_name}_tenant_id", table_name=table_name)
        op.drop_column(table_name, "tenant_id")
