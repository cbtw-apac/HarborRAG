"""Add PostgreSQL-backed resumable agent-run checkpoints.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_JSON = sa.JSON()
_IX_TENANT = "ix_agent_runs_tenant_id"
_IX_SESSION = "ix_agent_runs_session_id"
_IX_STATUS = "ix_agent_runs_status"


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("principal_id", sa.String(length=512), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state_json", _JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["conversation_sessions.session_id"],
            name=op.f("fk_agent_runs_session_id_conversation_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_agent_runs")),
    )
    op.create_index(_IX_TENANT, "agent_runs", ["tenant_id"])
    op.create_index(_IX_SESSION, "agent_runs", ["session_id"])
    op.create_index(_IX_STATUS, "agent_runs", ["status"])


def downgrade() -> None:
    op.drop_index(_IX_STATUS, table_name="agent_runs")
    op.drop_index(_IX_SESSION, table_name="agent_runs")
    op.drop_index(_IX_TENANT, table_name="agent_runs")
    op.drop_table("agent_runs")
