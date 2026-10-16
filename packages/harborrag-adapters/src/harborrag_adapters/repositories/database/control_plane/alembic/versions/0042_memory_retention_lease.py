"""Memory-retention purge: singleton lease row and list_expired indexes.

``try_acquire`` returns False for a lease name with no row, so without this
seed the purge would never run in production (see 0018 for the same lesson).
The two indexes serve the purge's oldest-first scan and explicit-expiry filter.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

_TZDT = sa.DateTime(timezone=True)

_SINGLETON_LEASES = sa.table(
    "singleton_leases",
    sa.column("name", sa.Text()),
    sa.column("holder", sa.Text()),
    sa.column("expires_at", _TZDT),
)

_LEASE_NAME = "memory_retention"
_UPDATED_INDEX = "ix_memories_updated_at_id"
_EXPIRES_INDEX = "ix_memories_expires_at"


def upgrade() -> None:
    op.bulk_insert(
        _SINGLETON_LEASES,
        [{"name": _LEASE_NAME, "holder": "", "expires_at": datetime(1970, 1, 1, tzinfo=UTC)}],
    )
    op.create_index(_UPDATED_INDEX, "memories", ["updated_at", "memory_id"])
    op.create_index(_EXPIRES_INDEX, "memories", ["expires_at"])


def downgrade() -> None:
    op.drop_index(_EXPIRES_INDEX, table_name="memories")
    op.drop_index(_UPDATED_INDEX, table_name="memories")
    op.execute(_SINGLETON_LEASES.delete().where(_SINGLETON_LEASES.c.name == _LEASE_NAME))
