"""Journal of right-to-erasure runs.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_TABLE = "user_erasures"
_INDEX = "ix_user_erasures_subject"
_JSON = sa.JSON()
_TZDT = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("erasure_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("subject_user_id", sa.String(512), nullable=False),
        sa.Column("actor", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("counts_json", _JSON, nullable=False),
        sa.Column("failed_steps_json", _JSON, nullable=False),
        sa.Column("started_at", _TZDT, nullable=False),
        sa.Column("finished_at", _TZDT, nullable=True),
    )
    op.create_index(_INDEX, _TABLE, ["tenant_id", "subject_user_id", "started_at"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
