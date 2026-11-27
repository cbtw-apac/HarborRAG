"""Schedules that run a connection operation on a timetable, and the slots they fire.

The stored schedule is the source of truth and the timer only fires slots, so a slot
whose schedule is missing or paused does nothing. Every slot and ``trigger`` starts
its run through ``IngestionStarter``, the code path behind ``POST
/v1/admin/ingestions``, acting as the schedule.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from harborrag_core.base import utc_now
from harborrag_core.config_store.durations import parse_iso8601_duration_seconds
from harborrag_core.config_store.levels import SettingsLevel
from harborrag_core.config_store.registry import SchedulesGroup
from harborrag_core.contracts.errors import (
    HarborAlreadyRunningError,
    HarborPreconditionFailedError,
    HarborUnavailableError,
    HarborValidationError,
    ScheduleNotFoundError,
    UnknownReferenceError,
)
from harborrag_core.domain.schedule import (
    RECENT_SLOT_LIMIT,
    SYSTEM_PAUSER,
    Schedule,
    SchedulePause,
    ScheduleSlot,
    SlotReason,
)
from harborrag_core.ingestion import IngestionTask
from harborrag_core.ports.control_plane import ScheduleRepositoryPort
from harborrag_engine.control_plane.connection_views import ANY_VERSION, ingestion_status
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.ingestion.connections import ConnectionUnavailableError
from harborrag_runtime.scheduling.validation import min_cron_gap_seconds

from ..ingestion.starter import IngestionStarter
from .connection_models import ConnectionKey, ConnectionRuns
from .schedule_models import (
    ScheduleKey,
    ScheduleQuery,
    ScheduleTimers,
    ScheduleWrite,
    SlotAttempt,
    SlotStart,
    WriteConditions,
)
from .schedule_rules import catch_up_window_seconds, parse_timetable
from .schedule_views import schedule_view

logger = logging.getLogger("harborrag.runtime.application.control_plane.schedules")

SCHEDULE_ACTOR_TYPE: Final = "schedule"
DEFAULT_PAUSE_REASON: Final = "Paused by an operator"
_ENDED_STATUSES: Final = frozenset({"succeeded", "failed", "cancelled"})
_TIMER_SYNC_ROUNDS: Final = 3


class ConnectionScheduleService:
    """The schedule use cases behind ``/v1/admin/schedules`` and the slots they fire.

    Args:
        control_plane: Control-plane repositories; reads ``schedules``, ``projects``,
            ``connections`` and ``config``.
        runs: Run registry, for the status of the runs slots started.
        starter: Starts ingestions through the ``POST /v1/admin/ingestions`` path.
        timers: Returns the engine that fires each schedule's slots.
    """

    def __init__(
        self,
        *,
        control_plane: Callable[[], ControlPlaneRepositories],
        runs: Callable[[], Awaitable[ConnectionRuns]],
        starter: IngestionStarter,
        timers: Callable[[], Awaitable[ScheduleTimers]],
    ) -> None:
        self._control_plane = control_plane
        self._runs = runs
        self._starter = starter
        self._timers = timers

    async def page(self, tenant_id: str, query: ScheduleQuery) -> dict[str, Any]:
        """Return a page of the tenant's schedules, newest first, each with its latest slot.

        Raises:
            HarborValidationError: ``query.cursor`` is malformed.
        """
        schedules, next_cursor = await self._schedules().page(
            tenant_id=tenant_id, filters=query.filters, limit=query.limit, cursor=query.cursor
        )
        return {
            "items": await self._views(tenant_id, schedules, slot_limit=1),
            "next_cursor": next_cursor,
        }

    async def get(self, key: ScheduleKey) -> tuple[dict[str, Any], int]:
        """Return a schedule with its last 20 slots, and its version.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
        """
        schedule = await self._require(key)
        (view,) = await self._views(key.tenant_id, [schedule], slot_limit=RECENT_SLOT_LIMIT)
        return view, schedule.version

    async def put(
        self, key: ScheduleKey, write: ScheduleWrite, *, actor: str, conditions: WriteConditions
    ) -> tuple[dict[str, Any], int, bool]:
        """Create or overwrite a schedule, then set its timer to the new timetable.

        An overwrite keeps the schedule's pause, failure count and slots. When the
        timer cannot be set, a schedule this call created is removed again.

        Args:
            key: The schedule to write.
            write: Its configuration.
            actor: Subject recorded as the author.
            conditions: ``If-Match`` and ``If-None-Match`` preconditions.

        Returns:
            The schedule view, its new version, and whether this call created it.

        Raises:
            HarborValidationError: A field breaks a timetable rule, or the connection
                already has ``schedules.max_per_connection`` schedules.
            UnknownReferenceError: The project or the connection does not exist.
            ConnectionUnavailableError: The connection is being deleted.
            ScheduleIdTakenError: ``create_only`` and the schedule exists.
            HarborPreconditionFailedError: ``expected_version`` does not hold.
            HarborUnavailableError: The timer engine cannot be reached.
        """
        settings = await self._settings(key.tenant_id)
        timetable = parse_timetable(
            write, min_interval_seconds=parse_iso8601_duration_seconds(settings.min_interval)
        )
        await self._check_connection(key, write, max_per_connection=settings.max_per_connection)
        expected_version = await self._expected_version(key, conditions)
        stored, created = await self._schedules().put(
            Schedule(
                tenant_id=key.tenant_id,
                schedule_id=key.schedule_id,
                project_id=write.project_id,
                connection_id=write.connection_id,
                mode=write.mode,
                description=write.description,
                interval_seconds=timetable.interval_seconds,
                cron=timetable.cron,
                timezone=timetable.timezone,
                jitter_seconds=timetable.jitter_seconds,
                if_running=write.if_running,
                catch_up=write.catch_up,
                pause_after_failures=write.pause_after_failures,
                created_by=actor,
                updated_by=actor,
            ),
            expected_version=expected_version,
            create_only=conditions.create_only,
        )
        await self._set_timer(stored, period_seconds=timetable.period_seconds, created=created)
        view, version = await self.get(key)
        return view, version, created

    async def delete(self, key: ScheduleKey) -> None:
        """Remove a schedule's timer, then the schedule; a run it started keeps going.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
            HarborUnavailableError: The timer engine cannot be reached.
        """
        await self._require(key)
        await self._remove(key)

    async def pause(self, key: ScheduleKey, *, reason: str | None, actor: str) -> dict[str, Any]:
        """Pause a schedule; pausing a paused one returns it unchanged.

        A run in progress keeps going.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
            HarborUnavailableError: The timer engine cannot be reached.
        """
        pause = SchedulePause(reason=reason or DEFAULT_PAUSE_REASON, by=actor, at=utc_now())
        stored = await self._set_paused(key, pause, actor=actor)
        note = stored.paused.reason if stored.paused is not None else pause.reason
        await (await self._timers()).pause(key.tenant_id, key.schedule_id, note=note)
        (view,) = await self._views(key.tenant_id, [stored], slot_limit=RECENT_SLOT_LIMIT)
        return view

    async def resume(self, key: ScheduleKey, *, actor: str) -> dict[str, Any]:
        """Resume a schedule and clear its failure count; resuming an active one changes nothing.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
            HarborUnavailableError: The timer engine cannot be reached.
        """
        await self._set_paused(key, None, actor=actor)
        next_run_at = await (await self._timers()).resume(key.tenant_id, key.schedule_id)
        await self._schedules().set_next_run_at(
            tenant_id=key.tenant_id, schedule_id=key.schedule_id, next_run_at=next_run_at
        )
        view, _ = await self.get(key)
        return view

    async def trigger(self, key: ScheduleKey) -> dict[str, Any]:
        """Start one run now with the schedule's input, even while it is paused.

        The slot is recorded as manual, and the run's outcome counts toward the
        auto-pause.

        Returns:
            The queued run as the spec's ``Ingestion`` object.

        Raises:
            ScheduleNotFoundError: No such schedule in the tenant.
            UnknownReferenceError: The project or the connection no longer exists.
            HarborAlreadyRunningError: The connection already has a run.
            ConnectionUnavailableError: The connection is being deleted.
        """
        schedule = await self._require(key)
        ingestion = await self._start(schedule)
        ingestion_id = str(ingestion["ingestion_id"])
        await self._record(
            schedule,
            ScheduleSlot(
                due_at=utc_now(), outcome="started", manual=True, ingestion_id=ingestion_id
            ),
        )
        try:
            await (await self._timers()).follow(key.tenant_id, key.schedule_id, ingestion_id)
        except HarborUnavailableError:
            logger.exception(
                "Could not follow a triggered run; its outcome will not count toward the "
                "auto-pause schedule_id=%s ingestion_id=%s",
                key.schedule_id,
                ingestion_id,
            )
        return ingestion

    async def remove_for_connection(self, key: ConnectionKey) -> None:
        """Remove every schedule of a connection before its data goes; safe to repeat.

        Raises:
            HarborUnavailableError: The timer engine cannot be reached.
        """
        repository = self._control_plane().schedules
        if repository is None:
            return
        for schedule in await repository.list_for_connection(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        ):
            await self._remove(ScheduleKey(schedule.tenant_id, schedule.schedule_id))
            logger.info(
                "Removed a schedule of a deleted connection schedule_id=%s",
                schedule.schedule_id,
            )

    async def run_slot(
        self,
        key: ScheduleKey,
        *,
        due_at: datetime,
        attempt: SlotAttempt = SlotAttempt.DUE,
        fired_at: datetime | None = None,
    ) -> SlotStart:
        """Run one slot: start the schedule's operation or record why it did not start.

        A slot that comes while the connection has a run is skipped, or with
        ``if_running: queue`` waits when no other slot does. A waiting slot that
        finds the connection busy again keeps waiting without a new record. A due
        slot that fires later than its catch-up window and jitter allow, as after an
        outage of every API replica, is ignored without a record.

        Args:
            key: The schedule whose slot came due.
            due_at: When the slot came due.
            attempt: ``DUE`` when the slot comes due, ``AFTER_WAIT`` once the run it
                waited for ended.
            fired_at: When a worker picked the slot up; ``None`` skips the lateness check.

        Returns:
            What the slot did and the run involved.
        """
        schedule = await self._schedules().get(tenant_id=key.tenant_id, schedule_id=key.schedule_id)
        if schedule is None or schedule.paused is not None:
            await self._release_queue(key, attempt)
            return SlotStart("ignored")
        if attempt is SlotAttempt.DUE and _fired_too_late(schedule, due_at, fired_at):
            logger.info(
                "Schedule slot fired after its catch-up window schedule_id=%s",
                schedule.schedule_id,
            )
            return SlotStart("ignored")
        if not (await self._settings(key.tenant_id)).enabled:
            await self._release_queue(key, attempt)
            return await self._skip(schedule, due_at, "disabled_by_setting", None)
        try:
            ingestion = await self._start(schedule)
        except HarborAlreadyRunningError as error:
            return await self._busy(schedule, due_at, _blocking_run(error), attempt)
        except ConnectionUnavailableError:
            await self._release_queue(key, attempt)
            return await self._skip(schedule, due_at, "connection_deleting", None)
        except UnknownReferenceError:
            await self._release_queue(key, attempt)
            return await self._skip(schedule, due_at, "reference_not_found", None)
        await self._release_queue(key, attempt)
        ingestion_id = str(ingestion["ingestion_id"])
        await self._record(
            schedule, ScheduleSlot(due_at=due_at, outcome="started", ingestion_id=ingestion_id)
        )
        return SlotStart("started", ingestion_id)

    async def refresh_next_run(self, key: ScheduleKey) -> None:
        """Store when the schedule's next slot fires, as its timer reports it."""
        next_run_at = await (await self._timers()).next_run_at(key.tenant_id, key.schedule_id)
        await self._schedules().set_next_run_at(
            tenant_id=key.tenant_id, schedule_id=key.schedule_id, next_run_at=next_run_at
        )

    async def run_ended(self, key: ScheduleKey, ingestion_id: str) -> bool:
        """Return whether a run has ended; a run that no longer exists has."""
        task = await (await self._runs()).get(ingestion_id)
        return (
            task is None
            or task.request.get("tenant_id") != key.tenant_id
            or ingestion_status(task.status) in _ENDED_STATUSES
        )

    async def finish_run(self, key: ScheduleKey, ingestion_id: str) -> None:
        """Count a finished run toward the auto-pause, pausing the schedule at its limit.

        A failed run adds one to ``consecutive_failures``, a successful run resets it,
        and a cancelled run leaves it. Reaching ``pause_after_failures`` pauses the
        schedule as ``system``.
        """
        task = await (await self._runs()).get(ingestion_id)
        if task is None or task.request.get("tenant_id") != key.tenant_id:
            return
        status = ingestion_status(task.status)
        if status not in {"succeeded", "failed"}:
            return
        failures = await self._schedules().record_run_outcome(
            tenant_id=key.tenant_id, schedule_id=key.schedule_id, failed=status == "failed"
        )
        schedule = await self._schedules().get(tenant_id=key.tenant_id, schedule_id=key.schedule_id)
        if schedule is None or not _reached_failure_limit(schedule, failures):
            return
        reason = f"Paused after {failures} failed runs in a row ({_error_category(task)})"
        pause = SchedulePause(reason=reason, by=SYSTEM_PAUSER, at=utc_now())
        await self._set_paused(key, pause, actor=SYSTEM_PAUSER)
        await (await self._timers()).pause(key.tenant_id, key.schedule_id, note=reason)
        logger.warning(
            "Schedule paused after failed runs schedule_id=%s failures=%d",
            key.schedule_id,
            failures,
        )

    async def release_waiting_slot(self, key: ScheduleKey) -> None:
        """Stop a slot from waiting, so a later slot of the schedule may wait instead."""
        await self._schedules().release_queue(tenant_id=key.tenant_id, schedule_id=key.schedule_id)

    async def _start(self, schedule: Schedule) -> dict[str, Any]:
        """Start the schedule's ingestion through the ``POST /v1/admin/ingestions`` path."""
        return await self._starter.start(
            ConnectionKey(schedule.tenant_id, schedule.project_id, schedule.connection_id),
            actor=schedule.schedule_id,
            actor_type=SCHEDULE_ACTOR_TYPE,
            mode=schedule.mode,
            item_refs=(),
            updated_after=None,
        )

    async def _busy(
        self, schedule: Schedule, due_at: datetime, blocking: str | None, attempt: SlotAttempt
    ) -> SlotStart:
        """Queue or skip a slot that found the connection with a run."""
        if attempt is SlotAttempt.AFTER_WAIT:
            return SlotStart("queued", blocking)
        if schedule.if_running == "queue" and await self._schedules().claim_queue(
            tenant_id=schedule.tenant_id, schedule_id=schedule.schedule_id
        ):
            await self._record_waiting(schedule, due_at, blocking)
            return SlotStart("queued", blocking)
        return await self._skip(schedule, due_at, "already_running", blocking)

    async def _record_waiting(
        self, schedule: Schedule, due_at: datetime, blocking: str | None
    ) -> None:
        """Record a slot that waits; give its place back when the record cannot be written."""
        slot = ScheduleSlot(
            due_at=due_at, outcome="queued", reason="already_running", ingestion_id=blocking
        )
        try:
            await self._record(schedule, slot)
        except BaseException:
            await self.release_waiting_slot(ScheduleKey(schedule.tenant_id, schedule.schedule_id))
            raise

    async def _skip(
        self, schedule: Schedule, due_at: datetime, reason: SlotReason, blocking: str | None
    ) -> SlotStart:
        """Record a skipped slot."""
        await self._record(
            schedule,
            ScheduleSlot(due_at=due_at, outcome="skipped", reason=reason, ingestion_id=blocking),
        )
        logger.info("Schedule slot skipped schedule_id=%s reason=%s", schedule.schedule_id, reason)
        return SlotStart("skipped", blocking)

    async def _record(self, schedule: Schedule, slot: ScheduleSlot) -> None:
        await self._schedules().record_slot(
            tenant_id=schedule.tenant_id, schedule_id=schedule.schedule_id, slot=slot
        )

    async def _release_queue(self, key: ScheduleKey, attempt: SlotAttempt) -> None:
        """Let a later slot wait once this one, which waited, is done."""
        if attempt is SlotAttempt.AFTER_WAIT:
            await self.release_waiting_slot(key)

    async def _set_timer(self, schedule: Schedule, *, period_seconds: int, created: bool) -> None:
        """Set the timer to the stored timetable; undo a create the timer could not follow.

        A write that lands while this one sets the timer may have set it first, so the
        timer is set again from the stored schedule until the version stops moving.
        """
        try:
            await self._put_timer(schedule, period_seconds=period_seconds)
        except HarborUnavailableError:
            if created:
                await self._schedules().delete(
                    tenant_id=schedule.tenant_id, schedule_id=schedule.schedule_id
                )
            raise
        for _ in range(_TIMER_SYNC_ROUNDS):
            current = await self._schedules().get(
                tenant_id=schedule.tenant_id, schedule_id=schedule.schedule_id
            )
            if current is None or current.version == schedule.version:
                return
            schedule = current
            await self._put_timer(schedule, period_seconds=_period_seconds(schedule))

    async def _put_timer(self, schedule: Schedule, *, period_seconds: int) -> None:
        """Set the timer to one stored schedule and store its next slot time."""
        window = catch_up_window_seconds(catch_up=schedule.catch_up, period_seconds=period_seconds)
        next_run_at = await (await self._timers()).put(schedule, catch_up_window_seconds=window)
        await self._schedules().set_next_run_at(
            tenant_id=schedule.tenant_id,
            schedule_id=schedule.schedule_id,
            next_run_at=next_run_at,
        )

    async def _remove(self, key: ScheduleKey) -> None:
        await (await self._timers()).delete(key.tenant_id, key.schedule_id)
        await self._schedules().delete(tenant_id=key.tenant_id, schedule_id=key.schedule_id)

    async def _set_paused(
        self, key: ScheduleKey, pause: SchedulePause | None, *, actor: str
    ) -> Schedule:
        stored = await self._schedules().set_paused(
            tenant_id=key.tenant_id, schedule_id=key.schedule_id, paused=pause, actor=actor
        )
        if stored is None:
            raise ScheduleNotFoundError(f"schedule {key.schedule_id!r} not found")
        return stored

    async def _check_connection(
        self, key: ScheduleKey, write: ScheduleWrite, *, max_per_connection: int
    ) -> None:
        """Refuse a missing or deleting connection, or one with too many schedules."""
        control_plane = self._control_plane()
        tenants = frozenset({key.tenant_id})
        if await control_plane.projects.get(write.project_id, tenant_ids=tenants) is None:
            raise UnknownReferenceError(
                f"project {write.project_id!r} not found", {"field": "input.project_id"}
            )
        connection = await control_plane.connections.get(
            tenant_id=key.tenant_id, project_id=write.project_id, connection_id=write.connection_id
        )
        if connection is None:
            raise UnknownReferenceError(
                f"connection {write.connection_id!r} not found", {"field": "input.connection_id"}
            )
        if connection.status == "deleting":
            raise ConnectionUnavailableError(f"connection {connection.ref!r} is being deleted")
        others = [
            schedule
            for schedule in await self._schedules().list_for_connection(
                tenant_id=key.tenant_id,
                project_id=write.project_id,
                connection_id=write.connection_id,
            )
            if schedule.schedule_id != key.schedule_id
        ]
        if len(others) >= max_per_connection:
            raise HarborValidationError(
                "the connection has too many schedules",
                {
                    "errors": [
                        {
                            "field": "input.connection_id",
                            "issue": f"the connection already has {len(others)} schedules, "
                            "the most schedules.max_per_connection allows",
                        }
                    ]
                },
            )

    async def _expected_version(self, key: ScheduleKey, conditions: WriteConditions) -> int | None:
        """Turn ``If-Match`` into the version the stored schedule must have."""
        if conditions.expected_version != ANY_VERSION:
            return conditions.expected_version
        current = await self._schedules().get(tenant_id=key.tenant_id, schedule_id=key.schedule_id)
        if current is None:
            raise HarborPreconditionFailedError("If-Match: * needs an existing schedule")
        return current.version

    async def _require(self, key: ScheduleKey) -> Schedule:
        schedule = await self._schedules().get(tenant_id=key.tenant_id, schedule_id=key.schedule_id)
        if schedule is None:
            raise ScheduleNotFoundError(f"schedule {key.schedule_id!r} not found")
        return schedule

    async def _views(
        self, tenant_id: str, schedules: Sequence[Schedule], *, slot_limit: int
    ) -> list[dict[str, Any]]:
        """Build each schedule's view with its newest slots and their runs' status."""
        slots = await self._schedules().recent_slots(
            tenant_id=tenant_id,
            schedule_ids=[schedule.schedule_id for schedule in schedules],
            limit=slot_limit,
        )
        runs = await self._slot_runs(
            tenant_id, [slot for listed in slots.values() for slot in listed]
        )
        enabled = (await self._settings(tenant_id)).enabled
        return [
            schedule_view(
                schedule, slots=slots.get(schedule.schedule_id, []), runs=runs, enabled=enabled
            )
            for schedule in schedules
        ]

    async def _slot_runs(
        self, tenant_id: str, slots: Sequence[ScheduleSlot]
    ) -> dict[str, IngestionTask]:
        """Read the runs slots name, keeping only the tenant's own.

        One read per run: a page shows one slot per schedule and a schedule shows 20.
        """
        registry = await self._runs()
        runs: dict[str, IngestionTask] = {}
        for ingestion_id in {slot.ingestion_id for slot in slots if slot.ingestion_id}:
            task = await registry.get(ingestion_id)
            if task is not None and task.request.get("tenant_id") == tenant_id:
                runs[ingestion_id] = task
        return runs

    async def _settings(self, tenant_id: str) -> SchedulesGroup:
        """Read the tenant's ``schedules.*`` settings, or their defaults without a store."""
        config = self._control_plane().config
        if config is None:
            return SchedulesGroup.model_validate({})
        effective = await config.effective(tenant_id, SettingsLevel("tenant"))
        return SchedulesGroup.model_validate(
            {name: effective.value(f"schedules.{name}") for name in SchedulesGroup.model_fields}
        )

    def _schedules(self) -> ScheduleRepositoryPort:
        repository = self._control_plane().schedules
        if repository is None:
            raise HarborUnavailableError("this deployment has no schedule store")
        return repository


def _fired_too_late(schedule: Schedule, due_at: datetime, fired_at: datetime | None) -> bool:
    """Whether a slot fired later than its catch-up window plus its jitter."""
    if fired_at is None:
        return False
    window = catch_up_window_seconds(
        catch_up=schedule.catch_up, period_seconds=_period_seconds(schedule)
    )
    return fired_at - due_at > timedelta(seconds=window + schedule.jitter_seconds)


def _period_seconds(schedule: Schedule) -> int:
    """The interval, or the shortest gap between the cron expression's runs."""
    if schedule.interval_seconds is not None:
        return schedule.interval_seconds
    return min_cron_gap_seconds(schedule.cron or "")


def _blocking_run(error: HarborAlreadyRunningError) -> str | None:
    """The run that made a slot skip or wait, from the conflict's details."""
    ingestion_id = error.details.get("ingestion_id")
    return str(ingestion_id) if ingestion_id else None


def _reached_failure_limit(schedule: Schedule, failures: int) -> bool:
    return (
        schedule.paused is None
        and schedule.pause_after_failures > 0
        and failures >= schedule.pause_after_failures
    )


def _error_category(task: IngestionTask) -> str:
    summary = task.summary
    category = summary.get("error_code") if isinstance(summary, dict) else None
    return str(category) if category else "unknown"
