"""Index the keyset the public ingestion task list pages over.

GET /v1/ingestions filters by the caller's tenant scope and walks
(submitted_at, task_id) descending. Only ix_ingestion_tasks_tenant_id existed,
so each page sorted the tenant's entire task history to return at most 200
rows; this makes the keyset an index scan instead.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_ingestion_tasks_tenant_submitted"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "ingestion_tasks",
        ["tenant_id", "submitted_at", "task_id"],
        postgresql_ops={"submitted_at": "DESC", "task_id": "DESC"},
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="ingestion_tasks")
