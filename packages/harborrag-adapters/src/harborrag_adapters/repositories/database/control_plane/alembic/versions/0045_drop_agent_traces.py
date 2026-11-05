"""Drop what only agent runs wrote: session kind, tool traffic, run ids and usage surface.

Revision ID: 0045
Revises: 0044

The upgrade deletes tool messages, the replay claims of agent sessions,
run-scoped memories and working memories, then drops their columns. Agent
sessions themselves stay and list as chat. The downgrade restores the columns
empty; the deleted rows cannot be recovered.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

_OWNER_INDEX = "ix_memories_owner"
_OWNER = ["tenant_id", "project_id", "principal_id", "session_id"]
_OWNER_WITH_RUN = [*_OWNER, "run_id"]


def upgrade() -> None:
    op.execute("DELETE FROM conversation_messages WHERE role = 'tool'")
    op.execute(
        "DELETE FROM completion_requests WHERE session_id IN "
        "(SELECT session_id FROM conversation_sessions WHERE kind = 'agent')"
    )
    op.execute("DELETE FROM memories WHERE scope = 'run' OR memory_type = 'working'")
    op.drop_index(_OWNER_INDEX, table_name="memories")
    with op.batch_alter_table("memories") as batch:
        batch.drop_column("run_id")
    op.create_index(_OWNER_INDEX, "memories", _OWNER)
    with op.batch_alter_table("conversation_messages") as batch:
        batch.drop_column("tool_calls_json")
        batch.drop_column("tool_call_id")
        batch.drop_column("run_id")
    with op.batch_alter_table("conversation_sessions") as batch:
        batch.drop_column("kind")
    with op.batch_alter_table("model_usage") as batch:
        batch.drop_column("surface")
        batch.drop_column("run_id")


def downgrade() -> None:
    with op.batch_alter_table("model_usage") as batch:
        batch.add_column(sa.Column("run_id", sa.String(length=128), nullable=True))
        batch.add_column(
            sa.Column("surface", sa.String(length=16), nullable=False, server_default="chat")
        )
    with op.batch_alter_table("model_usage") as batch:
        batch.alter_column("surface", existing_type=sa.String(length=16), server_default=None)
    with op.batch_alter_table("conversation_sessions") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="chat")
        )
    with op.batch_alter_table("conversation_messages") as batch:
        batch.add_column(sa.Column("tool_calls_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("tool_call_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("run_id", sa.String(length=128), nullable=True))
    op.drop_index(_OWNER_INDEX, table_name="memories")
    with op.batch_alter_table("memories") as batch:
        batch.add_column(sa.Column("run_id", sa.String(length=128), nullable=True))
    op.create_index(_OWNER_INDEX, "memories", _OWNER_WITH_RUN)
