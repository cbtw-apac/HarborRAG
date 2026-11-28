"""SQLAlchemy ``ScheduleRepositoryPort`` over the ``schedules`` and ``schedule_slots`` tables.

Every statement is scoped by ``tenant_id``. Runtime bookkeeping (failure count, next slot
time, queue marker, slots) updates its columns in place and never advances ``version``.
``record_slot`` keeps at most ``RECENT_SLOT_LIMIT`` slots per schedule, so ``recent_slots``
reads a schedule's slots whole and cuts them in memory.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, assert_never, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from harborrag_core.contracts.errors import (
    HarborPreconditionFailedError,
    HarborValidationError,
    ScheduleIdTakenError,
)
from harborrag_core.domain.schedule import (
    QUEUE_WAIT_LIMIT,
    RECENT_SLOT_LIMIT,
    Schedule,
    ScheduleFilter,
    SchedulePause,
    ScheduleSlot,
    ScheduleState,
)

from .mapping import utc_now
from .schemas_schedules import ScheduleRow, ScheduleSlotRow
from .session import SessionFactory

_STALE_QUEUE_MARK = QUEUE_WAIT_LIMIT + timedelta(hours=1)


@dataclass(slots=True)
class SqlScheduleRepository:
    """ScheduleRepositoryPort over the schedules and schedule_slots tables.

    Read-modify-write calls lock the schedule row, so concurrent writers to one
    schedule apply one after the other.

    Args:
        sessions: Session factory of the control-plane database.
    """

    sessions: SessionFactory

    async def get(self, *, tenant_id: str, schedule_id: str) -> Schedule | None:
        """One schedule, or None."""
        async with self.sessions() as session:
            row = await session.get(ScheduleRow, (tenant_id, schedule_id))
            return _to_domain(row) if row is not None else None

    async def page(
        self, *, tenant_id: str, filters: ScheduleFilter, limit: int, cursor: str | None
    ) -> tuple[list[Schedule], str | None]:
        """Newest ``created_at`` first, then by ``schedule_id``, walked by a keyset cursor.

        Raises:
            HarborValidationError: ``cursor`` is malformed.
        """
        conditions = [ScheduleRow.tenant_id == tenant_id, *_matching(filters)]
        if cursor is not None:
            conditions.append(_after(*_decode_cursor(cursor)))
        statement = (
            sa.select(ScheduleRow)
            .where(*conditions)
            .order_by(ScheduleRow.created_at.desc(), ScheduleRow.schedule_id)
            .limit(limit + 1)
        )
        async with self.sessions() as session:
            rows = list(await session.scalars(statement))
        page = rows[:limit]
        if len(rows) <= limit:
            return [_to_domain(row) for row in page], None
        return [_to_domain(row) for row in page], _encode_cursor(page[-1])

    async def list_for_connection(
        self, *, tenant_id: str, project_id: str, connection_id: str
    ) -> list[Schedule]:
        """Every schedule of one connection, oldest first."""
        statement = (
            sa.select(ScheduleRow)
            .where(
                ScheduleRow.tenant_id == tenant_id,
                ScheduleRow.project_id == project_id,
                ScheduleRow.connection_id == connection_id,
            )
            .order_by(ScheduleRow.created_at, ScheduleRow.schedule_id)
        )
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [_to_domain(row) for row in rows]

    async def next_runs(
        self, *, tenant_id: str, connections: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], datetime]:
        """The earliest next slot time of each listed connection, in one grouped query."""
        if not connections:
            return {}
        connection = sa.tuple_(ScheduleRow.project_id, ScheduleRow.connection_id)
        statement = (
            sa.select(
                ScheduleRow.project_id,
                ScheduleRow.connection_id,
                sa.func.min(ScheduleRow.next_run_at),
            )
            .where(
                ScheduleRow.tenant_id == tenant_id,
                ScheduleRow.paused_at.is_(None),
                ScheduleRow.next_run_at.is_not(None),
                connection.in_(list(connections)),
            )
            .group_by(ScheduleRow.project_id, ScheduleRow.connection_id)
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).all()
        return {
            (project_id, connection_id): earliest for project_id, connection_id, earliest in rows
        }

    async def put(
        self, schedule: Schedule, *, expected_version: int | None, create_only: bool
    ) -> tuple[Schedule, bool]:
        """Create or overwrite a schedule under the port's precondition rules.

        A create stores the schedule's own creation stamps; an overwrite stamps
        ``updated_at`` with the current time.

        Raises:
            ScheduleIdTakenError: ``create_only`` and the schedule exists, or a
                concurrent create took the ID first.
            HarborPreconditionFailedError: ``expected_version`` does not hold.
        """
        try:
            async with self.sessions.begin() as session:
                row = await session.get(
                    ScheduleRow, (schedule.tenant_id, schedule.schedule_id), with_for_update=True
                )
                if row is None:
                    if expected_version is not None:
                        raise HarborPreconditionFailedError("schedule does not exist")
                    row = _new_row(schedule)
                    session.add(row)
                    await session.flush()
                    return _to_domain(row), True
                if create_only:
                    raise _id_taken(schedule)
                if expected_version is not None and expected_version != row.version:
                    raise HarborPreconditionFailedError("schedule version does not match")
                _overwrite(row, schedule)
                return _to_domain(row), False
        except IntegrityError as error:
            raise _id_taken(schedule) from error

    async def set_paused(
        self, *, tenant_id: str, schedule_id: str, paused: SchedulePause | None, actor: str
    ) -> Schedule | None:
        """Pause or resume a schedule; the row stays locked until the change commits."""
        async with self.sessions.begin() as session:
            row = await session.get(ScheduleRow, (tenant_id, schedule_id), with_for_update=True)
            if row is None:
                return None
            if (paused is None) != (row.paused_at is None):
                _apply_pause(row, paused)
                row.version += 1
                row.updated_by = actor
                row.updated_at = utc_now()
            return _to_domain(row)

    async def set_next_run_at(
        self, *, tenant_id: str, schedule_id: str, next_run_at: datetime | None
    ) -> None:
        """Store the next slot time; a missing schedule is a no-op."""
        await self._changed(_update_one(tenant_id, schedule_id).values(next_run_at=next_run_at))

    async def record_run_outcome(self, *, tenant_id: str, schedule_id: str, failed: bool) -> int:
        """Count a failed run or reset the count in one UPDATE; 0 for a missing schedule."""
        failures = ScheduleRow.consecutive_failures + 1 if failed else 0
        statement = (
            _update_one(tenant_id, schedule_id)
            .values(consecutive_failures=failures)
            .returning(ScheduleRow.consecutive_failures)
        )
        async with self.sessions.begin() as session:
            stored = (await session.execute(statement)).scalar_one_or_none()
        return stored or 0

    async def claim_queue(self, *, tenant_id: str, schedule_id: str) -> bool:
        """Set the waiting-slot mark in one conditional UPDATE; False when held or missing."""
        now = utc_now()
        return await self._changed(
            _update_one(tenant_id, schedule_id)
            .where(
                sa.or_(
                    ScheduleRow.queued_at.is_(None),
                    ScheduleRow.queued_at < now - _STALE_QUEUE_MARK,
                )
            )
            .values(queued_at=now)
        )

    async def release_queue(self, *, tenant_id: str, schedule_id: str) -> None:
        """Clear the waiting-slot mark; a missing schedule is a no-op."""
        await self._changed(_update_one(tenant_id, schedule_id).values(queued_at=None))

    async def record_slot(self, *, tenant_id: str, schedule_id: str, slot: ScheduleSlot) -> None:
        """Append a slot and prune older ones in one transaction under the schedule's lock."""
        async with self.sessions.begin() as session:
            version = await session.scalar(
                sa.select(ScheduleRow.version)
                .where(_one_schedule(tenant_id, schedule_id))
                .with_for_update()
            )
            if version is None:
                return
            await session.execute(
                sa.insert(ScheduleSlotRow).values(
                    tenant_id=tenant_id,
                    schedule_id=schedule_id,
                    due_at=slot.due_at,
                    outcome=slot.outcome,
                    reason=slot.reason,
                    manual=slot.manual,
                    ingestion_id=slot.ingestion_id,
                    recorded_at=utc_now(),
                )
            )
            await session.execute(_prune_slots(tenant_id, schedule_id))

    async def recent_slots(
        self, *, tenant_id: str, schedule_ids: Sequence[str], limit: int
    ) -> dict[str, list[ScheduleSlot]]:
        """The newest ``limit`` slots of each listed schedule, newest first."""
        if not schedule_ids or limit < 1:
            return {}
        statement = (
            sa.select(ScheduleSlotRow)
            .where(
                ScheduleSlotRow.tenant_id == tenant_id,
                ScheduleSlotRow.schedule_id.in_(schedule_ids),
            )
            .order_by(ScheduleSlotRow.id.desc())
        )
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            slots: dict[str, list[ScheduleSlot]] = {}
            for row in rows:
                listed = slots.setdefault(row.schedule_id, [])
                if len(listed) < limit:
                    listed.append(_slot_to_domain(row))
        return slots

    async def delete(self, *, tenant_id: str, schedule_id: str) -> bool:
        """Remove the slots, then the schedule; return whether the schedule existed."""
        async with self.sessions.begin() as session:
            await session.execute(
                sa.delete(ScheduleSlotRow).where(_slots_of(tenant_id, schedule_id))
            )
            result = cast(
                "CursorResult[Any]",  # a DELETE runs through a cursor, which carries rowcount
                await session.execute(
                    sa.delete(ScheduleRow).where(_one_schedule(tenant_id, schedule_id))
                ),
            )
        return bool(result.rowcount)

    async def _changed(self, statement: sa.Update) -> bool:
        """Run one UPDATE in its own transaction; return whether it matched a row."""
        async with self.sessions.begin() as session:
            result = cast(
                "CursorResult[Any]",  # an UPDATE runs through a cursor, which carries rowcount
                await session.execute(statement),
            )
        return bool(result.rowcount)


def _one_schedule(tenant_id: str, schedule_id: str) -> sa.ColumnElement[bool]:
    return sa.and_(ScheduleRow.tenant_id == tenant_id, ScheduleRow.schedule_id == schedule_id)


def _slots_of(tenant_id: str, schedule_id: str) -> sa.ColumnElement[bool]:
    return sa.and_(
        ScheduleSlotRow.tenant_id == tenant_id, ScheduleSlotRow.schedule_id == schedule_id
    )


def _update_one(tenant_id: str, schedule_id: str) -> sa.Update:
    return sa.update(ScheduleRow).where(_one_schedule(tenant_id, schedule_id))


def _prune_slots(tenant_id: str, schedule_id: str) -> sa.Delete:
    """Delete every slot of the schedule except the newest ``RECENT_SLOT_LIMIT``."""
    kept = (
        sa.select(ScheduleSlotRow.id)
        .where(_slots_of(tenant_id, schedule_id))
        .order_by(ScheduleSlotRow.id.desc())
        .limit(RECENT_SLOT_LIMIT)
    )
    return (
        sa.delete(ScheduleSlotRow)
        .where(_slots_of(tenant_id, schedule_id), ScheduleSlotRow.id.not_in(kept))
        .execution_options(synchronize_session=False)
    )


def _matching(filters: ScheduleFilter) -> list[sa.ColumnElement[bool]]:
    """Return one condition per set filter field."""
    conditions: list[sa.ColumnElement[bool]] = []
    if filters.project_id is not None:
        conditions.append(ScheduleRow.project_id == filters.project_id)
    if filters.connection_id is not None:
        conditions.append(ScheduleRow.connection_id == filters.connection_id)
    if filters.operation is not None:
        conditions.append(ScheduleRow.operation == filters.operation)
    if filters.state is not None:
        conditions.append(_in_state(filters.state))
    if filters.due_before is not None:
        conditions.append(ScheduleRow.next_run_at < filters.due_before)
    return conditions


def _in_state(state: ScheduleState) -> sa.ColumnElement[bool]:
    match state:
        case "active":
            return ScheduleRow.paused_at.is_(None)
        case "paused":
            return ScheduleRow.paused_at.is_not(None)
        case _:
            assert_never(state)


def _after(created_at: datetime, schedule_id: str) -> sa.ColumnElement[bool]:
    """Keyset predicate for rows after ``(created_at, schedule_id)`` in listing order."""
    return sa.or_(
        ScheduleRow.created_at < created_at,
        sa.and_(ScheduleRow.created_at == created_at, ScheduleRow.schedule_id > schedule_id),
    )


def _encode_cursor(last: ScheduleRow) -> str:
    """Encode the sort key of a page's last row as an opaque cursor."""
    payload = json.dumps([last.created_at.isoformat(), last.schedule_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    """Decode a cursor produced by ``_encode_cursor``.

    Raises:
        HarborValidationError: The cursor is not one this module produced.
    """
    padding = "=" * (-len(value) % 4)
    try:
        created_at, schedule_id = json.loads(base64.urlsafe_b64decode(value + padding))
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None or not isinstance(schedule_id, str) or not schedule_id:
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HarborValidationError("schedule cursor is invalid", {"field": "cursor"}) from error
    return created, schedule_id


def _id_taken(schedule: Schedule) -> ScheduleIdTakenError:
    return ScheduleIdTakenError(f"schedule {schedule.schedule_id!r} already exists")


def _new_row(schedule: Schedule) -> ScheduleRow:
    """Map a new schedule onto a fresh row at version 1 with no runtime state."""
    row = ScheduleRow(
        tenant_id=schedule.tenant_id,
        schedule_id=schedule.schedule_id,
        paused_reason=None,
        paused_by=None,
        paused_at=None,
        consecutive_failures=0,
        next_run_at=None,
        queued_at=None,
        version=1,
        created_by=schedule.created_by,
        updated_by=schedule.updated_by,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )
    _write_configuration(row, schedule)
    return row


def _overwrite(row: ScheduleRow, schedule: Schedule) -> None:
    """Replace the configuration, advance ``version`` and stamp the update."""
    _write_configuration(row, schedule)
    row.version += 1
    row.updated_by = schedule.updated_by
    row.updated_at = utc_now()


def _write_configuration(row: ScheduleRow, schedule: Schedule) -> None:
    """Copy the fields a schedule write sets; runtime state and stamps stay as they are."""
    row.project_id = schedule.project_id
    row.connection_id = schedule.connection_id
    row.operation = schedule.operation
    row.mode = schedule.mode
    row.description = schedule.description
    row.interval_seconds = schedule.interval_seconds
    row.cron = schedule.cron
    row.timezone = schedule.timezone
    row.jitter_seconds = schedule.jitter_seconds
    row.if_running = schedule.if_running
    row.catch_up = schedule.catch_up
    row.pause_after_failures = schedule.pause_after_failures


def _apply_pause(row: ScheduleRow, paused: SchedulePause | None) -> None:
    """Record a pause and clear the next slot time, or clear the pause and the failures."""
    if paused is None:
        row.paused_reason = None
        row.paused_by = None
        row.paused_at = None
        row.consecutive_failures = 0
        return
    row.paused_reason = paused.reason
    row.paused_by = paused.by
    row.paused_at = paused.at
    row.next_run_at = None


def _pause_of(row: ScheduleRow) -> SchedulePause | None:
    if row.paused_at is None:
        return None
    return SchedulePause(reason=row.paused_reason or "", by=row.paused_by or "", at=row.paused_at)


def _to_domain(row: ScheduleRow) -> Schedule:
    """Map a ``schedules`` row to its ``Schedule``."""
    return Schedule(
        tenant_id=row.tenant_id,
        schedule_id=row.schedule_id,
        project_id=row.project_id,
        connection_id=row.connection_id,
        operation=row.operation,
        mode=row.mode,
        description=row.description,
        interval_seconds=row.interval_seconds,
        cron=row.cron,
        timezone=row.timezone,
        jitter_seconds=row.jitter_seconds,
        if_running=row.if_running,
        catch_up=row.catch_up,
        pause_after_failures=row.pause_after_failures,
        paused=_pause_of(row),
        consecutive_failures=row.consecutive_failures,
        next_run_at=row.next_run_at,
        version=row.version,
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _slot_to_domain(row: ScheduleSlotRow) -> ScheduleSlot:
    """Map a ``schedule_slots`` row to its ``ScheduleSlot``."""
    return ScheduleSlot(
        due_at=row.due_at,
        outcome=row.outcome,
        reason=row.reason,
        manual=row.manual,
        ingestion_id=row.ingestion_id,
    )
