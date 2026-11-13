"""Store a chat session's project and an answer's mode, trace and feedback.

Revision ID: 0048
Revises: 0047

Phase: expand
Reversible: yes; the downgrade drops the four columns and the values stored in them
Touches: conversation_sessions (project_id), conversation_messages (mode, trace_id, feedback); nullable, no backfill
Locks: brief lock on conversation_sessions and conversation_messages to add or drop nullable
       columns; lock_timeout 5s on Postgres in both directions
Deploy: before the application release
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

_SESSIONS = "conversation_sessions"
_MESSAGES = "conversation_messages"


def _bound_lock_wait() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _bound_lock_wait()
    op.add_column(_SESSIONS, sa.Column("project_id", sa.String(128), nullable=True))
    op.add_column(_MESSAGES, sa.Column("mode", sa.String(16), nullable=True))
    op.add_column(_MESSAGES, sa.Column("trace_id", sa.String(64), nullable=True))
    op.add_column(_MESSAGES, sa.Column("feedback", sa.String(8), nullable=True))


def downgrade() -> None:
    _bound_lock_wait()
    with op.batch_alter_table(_MESSAGES) as batch:
        batch.drop_column("feedback")
        batch.drop_column("trace_id")
        batch.drop_column("mode")
    with op.batch_alter_table(_SESSIONS) as batch:
        batch.drop_column("project_id")
