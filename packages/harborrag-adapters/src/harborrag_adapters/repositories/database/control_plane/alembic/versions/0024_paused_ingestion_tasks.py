"""Add PAUSED to the public ingestion task lifecycle.

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ingestion_tasks") as batch_op:
        batch_op.drop_constraint("ck_ingestion_task_status", type_="check")
        batch_op.create_check_constraint(
            "ck_ingestion_task_status",
            "status IN ('PENDING','RUNNING','PAUSED','COMPLETED','PARTIAL','FAILED','CANCELLED')",
        )


def downgrade() -> None:
    with op.batch_alter_table("ingestion_tasks") as batch_op:
        batch_op.drop_constraint("ck_ingestion_task_status", type_="check")
        batch_op.create_check_constraint(
            "ck_ingestion_task_status",
            "status IN ('PENDING','RUNNING','COMPLETED','PARTIAL','FAILED','CANCELLED')",
        )
