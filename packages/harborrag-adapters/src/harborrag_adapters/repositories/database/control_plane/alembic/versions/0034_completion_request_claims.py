"""Persist owner-scoped completion idempotency claims and responses.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "completion_requests",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(512), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_completion_requests_owner_session",
        "completion_requests",
        ["tenant_id", "user_id", "session_id"],
    )


def downgrade() -> None:
    op.drop_table("completion_requests")
