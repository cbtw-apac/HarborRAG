"""Add schedules and schedule_slots: connection operations on a timetable and their recent slots.

Revision ID: 0050
Revises: 0049

Phase: expand
Reversible: yes; the downgrade drops both tables and the stored schedules and slots with them
Touches: schedules, schedule_slots (new, empty)
Locks: none
Deploy: before the application release
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

_TZDT = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("schedule_id", sa.String(63), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Text(), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("cron", sa.Text(), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("jitter_seconds", sa.Integer(), nullable=False),
        sa.Column("if_running", sa.String(8), nullable=False),
        sa.Column("catch_up", sa.Boolean(), nullable=False),
        sa.Column("pause_after_failures", sa.Integer(), nullable=False),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("paused_by", sa.String(256), nullable=True),
        sa.Column("paused_at", _TZDT, nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("next_run_at", _TZDT, nullable=True),
        sa.Column("queued_at", _TZDT, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("updated_by", sa.String(256), nullable=False),
        sa.Column("created_at", _TZDT, nullable=False),
        sa.Column("updated_at", _TZDT, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "schedule_id", name="pk_schedules"),
        sa.CheckConstraint(
            "(interval_seconds IS NULL) <> (cron IS NULL)", name="ck_schedules_timetable"
        ),
    )
    op.create_index(
        "ix_schedules_connection", "schedules", ["tenant_id", "project_id", "connection_id"]
    )
    op.create_table(
        "schedule_slots",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("schedule_id", sa.String(63), nullable=False),
        sa.Column("due_at", _TZDT, nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.Column("manual", sa.Boolean(), nullable=False),
        sa.Column("ingestion_id", sa.Text(), nullable=True),
        sa.Column("recorded_at", _TZDT, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_slots"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "schedule_id"],
            ["schedules.tenant_id", "schedules.schedule_id"],
            name="fk_schedule_slots_tenant_id_schedules",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_schedule_slots_schedule", "schedule_slots", ["tenant_id", "schedule_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_schedule_slots_schedule", table_name="schedule_slots")
    op.drop_table("schedule_slots")
    op.drop_index("ix_schedules_connection", table_name="schedules")
    op.drop_table("schedules")
