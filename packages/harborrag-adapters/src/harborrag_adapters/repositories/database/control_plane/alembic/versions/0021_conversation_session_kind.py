"""Bind each conversation session to the surface that created it.

Chat and agent completions persist turns into the same conversation_memory
table, and a session created through /v1/chat/sessions could be used with
/v1/agent/completions (and vice versa), interleaving both surfaces' turns in
the history recalled for the next prompt. ``kind`` ("chat" | "agent") records
the creating surface so a completion on the other surface is rejected as an
unknown session. Existing rows default to "chat".

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="chat"),
    )


def downgrade() -> None:
    op.drop_column("conversation_sessions", "kind")
