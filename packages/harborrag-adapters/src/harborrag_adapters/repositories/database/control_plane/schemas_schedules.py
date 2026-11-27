"""Schedule ORM rows (migration 0050).

Kept in its own module and imported from schemas.py so the tables register on
the shared ``Base`` metadata.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime
from harborrag_core.domain.schedule import (
    IfRunning,
    ScheduleMode,
    ScheduleOperation,
    SlotOutcome,
    SlotReason,
)

from .schemas import Base


class ScheduleRow(Base):
    """One schedule: a connection operation, its timetable and its runtime state.

    ``paused_at`` set means paused. ``queued_at`` marks when one slot started to wait
    for the connection's current run.
    """

    __tablename__ = "schedules"
    __table_args__ = (
        sa.CheckConstraint("(interval_seconds IS NULL) <> (cron IS NULL)", name="timetable"),
        sa.Index("ix_schedules_connection", "tenant_id", "project_id", "connection_id"),
    )

    tenant_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(sa.String(63), primary_key=True)
    project_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    connection_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    operation: Mapped[ScheduleOperation] = mapped_column(sa.String(32), nullable=False)
    mode: Mapped[ScheduleMode] = mapped_column(sa.String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    cron: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    timezone: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    jitter_seconds: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    if_running: Mapped[IfRunning] = mapped_column(sa.String(8), nullable=False)
    catch_up: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    pause_after_failures: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    paused_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    paused_by: Mapped[str | None] = mapped_column(sa.String(256), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    updated_by: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ScheduleSlotRow(Base):
    """One recorded slot of a schedule; a higher ``id`` is a newer slot."""

    __tablename__ = "schedule_slots"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["tenant_id", "schedule_id"],
            ["schedules.tenant_id", "schedules.schedule_id"],
            ondelete="CASCADE",
        ),
        sa.Index("ix_schedule_slots_schedule", "tenant_id", "schedule_id", "id"),
    )

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    tenant_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    schedule_id: Mapped[str] = mapped_column(sa.String(63), nullable=False)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    outcome: Mapped[SlotOutcome] = mapped_column(sa.String(16), nullable=False)
    reason: Mapped[SlotReason | None] = mapped_column(sa.String(32), nullable=True)
    manual: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    ingestion_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
