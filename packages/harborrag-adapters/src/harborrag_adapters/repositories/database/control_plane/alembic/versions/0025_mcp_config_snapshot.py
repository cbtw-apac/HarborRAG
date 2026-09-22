"""Single-document MCP effective-configuration snapshot (ML4-P3).

Published by harborrag-mcp-server through the runtime bridge whenever its
configuration is loaded or changes; harborrag-app reads it back for
``GET /v1/mcp/config`` since the layering rules forbid importing
harborrag-mcp-server directly to reach the live in-process store. Kept as
its own table rather than folded into ``workspace_settings`` -- that table's
``put`` replaces the whole document, and MCP publishing must never be able
to clobber unrelated workspace settings.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_JSON = sa.JSON()


def upgrade() -> None:
    op.create_table(
        "mcp_config_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("policy_json", _JSON, nullable=False),
        sa.Column("disabled_tools_json", _JSON, nullable=False),
        sa.Column("enabled_tool_count", sa.Integer(), nullable=False),
        sa.Column("total_tool_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("restart_required", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mcp_config_snapshot")
