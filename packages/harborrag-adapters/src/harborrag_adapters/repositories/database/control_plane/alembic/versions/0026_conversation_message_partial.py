"""Flag a conversation message whose text is only what a stream delivered.

An interrupted stream now persists the text the user already saw instead of
discarding the turn. Without this column that text is indistinguishable from
a finished answer, which is worse than losing it: a later turn would replay a
truncated answer as if the assistant had meant to stop there. Existing rows
predate interrupted-turn persistence, so they are complete by definition and
the server default of false is the correct backfill.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_TABLE = "conversation_messages"
_COLUMN = "partial"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
