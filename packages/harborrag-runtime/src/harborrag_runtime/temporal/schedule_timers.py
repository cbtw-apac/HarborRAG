"""Temporal Schedules as the timers of ``/v1/admin/schedules``.

A timer carries only the schedule's tenant and ID; its memo key differs from the
declared schedules' ``harborrag_schedule``, so their listing and pruning never see
it. Every ``RPCError`` leaves here as ``ScheduleTimerUnavailableError`` with IDs only.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final

from temporalio.client import (
    Client,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleHandle,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.client import Schedule as TemporalSchedule
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.domain.schedule import Schedule
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import RuntimeConnectionError

from .connection import connect_temporal_client
from .schedule_slots import SLOT_WORKFLOW, ScheduleSlotInput

MEMO_KEY: Final = "harborrag_api_schedule"

logger = logging.getLogger("harborrag.runtime.temporal.schedule_timers")


class ScheduleTimerUnavailableError(HarborUnavailableError):
    """Temporal, which fires schedule slots, cannot be reached or refused a call."""


class TemporalScheduleTimers:
    """``ScheduleTimers`` backed by one Temporal Schedule per stored schedule.

    The Temporal Schedule ID is ``harborrag-schedule:{tenant_id}:{schedule_id}``,
    since Temporal IDs are global. Each slot starts ``harborrag.schedule_slot`` with
    overlap allowed; the slot itself skips or queues.

    Args:
        client: Connected Temporal client.
        task_queue: Queue the API's slot worker polls.
    """

    def __init__(self, client: Client, *, task_queue: str) -> None:
        self._client = client
        self._task_queue = task_queue

    async def put(self, schedule: Schedule, *, catch_up_window_seconds: int) -> datetime | None:
        """Create the Temporal Schedule, or replace an existing one's whole configuration.

        Once the timer is set, a failure to read its next slot returns ``None``: the
        first slot stores the real time.

        Raises:
            ScheduleTimerUnavailableError: Temporal cannot be reached or refused the call.
        """
        desired = self._temporal_schedule(schedule, catch_up_window_seconds)
        temporal_id = _temporal_id(schedule.tenant_id, schedule.schedule_id)
        try:
            await self._client.create_schedule(
                temporal_id,
                desired,
                memo={
                    MEMO_KEY: {"tenant_id": schedule.tenant_id, "schedule_id": schedule.schedule_id}
                },
            )
        except ScheduleAlreadyRunningError:
            await self._replace(temporal_id, desired)
        except RPCError as error:
            raise _unavailable("create", temporal_id) from error
        return await self._next_run_once_set(schedule.tenant_id, schedule.schedule_id)

    async def pause(self, tenant_id: str, schedule_id: str, *, note: str) -> None:
        """Pause the Temporal Schedule; a missing one is a no-op."""
        temporal_id = _temporal_id(tenant_id, schedule_id)
        try:
            await self._handle(temporal_id).pause(note=note)
        except RPCError as error:
            _raise_unless_missing(error, "pause", temporal_id)

    async def resume(self, tenant_id: str, schedule_id: str) -> datetime | None:
        """Unpause the Temporal Schedule and return its next slot; ``None`` when it is missing."""
        temporal_id = _temporal_id(tenant_id, schedule_id)
        try:
            await self._handle(temporal_id).unpause()
        except RPCError as error:
            _raise_unless_missing(error, "unpause", temporal_id)
            return None
        return await self._next_run_once_set(tenant_id, schedule_id)

    async def next_run_at(self, tenant_id: str, schedule_id: str) -> datetime | None:
        """The next slot time Temporal reports; ``None`` while paused or when missing."""
        temporal_id = _temporal_id(tenant_id, schedule_id)
        try:
            description = await self._handle(temporal_id).describe()
        except RPCError as error:
            _raise_unless_missing(error, "describe", temporal_id)
            return None
        if description.schedule.state.paused:
            return None
        return next(iter(description.info.next_action_times), None)

    async def delete(self, tenant_id: str, schedule_id: str) -> None:
        """Delete the Temporal Schedule; a missing one is a no-op."""
        temporal_id = _temporal_id(tenant_id, schedule_id)
        try:
            await self._handle(temporal_id).delete()
        except RPCError as error:
            _raise_unless_missing(error, "delete", temporal_id)

    async def follow(self, tenant_id: str, schedule_id: str, ingestion_id: str) -> None:
        """Start a slot workflow that follows a triggered run; a repeat is a no-op.

        The workflow ID never repeats, so a run is counted toward the auto-pause once.
        """
        workflow_id = f"harborrag-schedule-follow:{tenant_id}:{schedule_id}:{ingestion_id}"
        try:
            await self._client.start_workflow(
                SLOT_WORKFLOW,
                ScheduleSlotInput(
                    tenant_id=tenant_id, schedule_id=schedule_id, ingestion_id=ingestion_id
                ),
                id=workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return
        except RPCError as error:
            raise _unavailable("start", workflow_id) from error

    async def _next_run_once_set(self, tenant_id: str, schedule_id: str) -> datetime | None:
        """Read the next slot of a timer just set; ``None`` when Temporal cannot say.

        The timer is already in place, so failing here would make the caller undo a
        stored schedule whose timer keeps firing.
        """
        try:
            return await self.next_run_at(tenant_id, schedule_id)
        except ScheduleTimerUnavailableError:
            logger.warning(
                "Could not read the next slot of a timer just set temporal_id=%s",
                _temporal_id(tenant_id, schedule_id),
            )
            return None

    def _temporal_schedule(
        self, schedule: Schedule, catch_up_window_seconds: int
    ) -> TemporalSchedule:
        """Translate the stored schedule into the whole Temporal Schedule."""
        tenant_id, schedule_id = schedule.tenant_id, schedule.schedule_id
        return TemporalSchedule(
            action=ScheduleActionStartWorkflow(
                SLOT_WORKFLOW,
                ScheduleSlotInput(tenant_id=tenant_id, schedule_id=schedule_id),
                id=f"harborrag-schedule-slot:{tenant_id}:{schedule_id}",
                task_queue=self._task_queue,
            ),
            spec=_spec(schedule),
            policy=SchedulePolicy(
                overlap=ScheduleOverlapPolicy.ALLOW_ALL,
                catchup_window=timedelta(seconds=catch_up_window_seconds),
            ),
            state=ScheduleState(
                paused=schedule.paused is not None,
                note=schedule.paused.reason if schedule.paused is not None else None,
            ),
        )

    async def _replace(self, temporal_id: str, desired: TemporalSchedule) -> None:
        """Replace an existing Temporal Schedule's action, spec, policy and pause state."""

        def updater(update: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(
                schedule=replace(
                    update.description.schedule,
                    action=desired.action,
                    spec=desired.spec,
                    policy=desired.policy,
                    state=desired.state,
                )
            )

        try:
            await self._handle(temporal_id).update(updater)
        except RPCError as error:
            raise _unavailable("update", temporal_id) from error

    def _handle(self, temporal_id: str) -> ScheduleHandle:
        return self._client.get_schedule_handle(temporal_id)


async def connect_schedule_timers(settings: RuntimeSettings) -> TemporalScheduleTimers:
    """Connect to Temporal and return the timers on ``settings.schedule_task_queue``.

    Raises:
        ScheduleTimerUnavailableError: Temporal cannot be reached.
    """
    try:
        client = await connect_temporal_client(TemporalRuntimeConfig.from_settings(settings))
    except RuntimeConnectionError as error:
        raise ScheduleTimerUnavailableError("could not connect to Temporal") from error
    return TemporalScheduleTimers(client, task_queue=settings.schedule_task_queue)


def _temporal_id(tenant_id: str, schedule_id: str) -> str:
    return f"harborrag-schedule:{tenant_id}:{schedule_id}"


def _spec(schedule: Schedule) -> ScheduleSpec:
    """The timetable: an interval, or a cron expression in its time zone, with jitter."""
    return ScheduleSpec(
        intervals=(
            [ScheduleIntervalSpec(every=timedelta(seconds=schedule.interval_seconds))]
            if schedule.interval_seconds is not None
            else []
        ),
        cron_expressions=[schedule.cron] if schedule.cron is not None else [],
        time_zone_name=schedule.timezone if schedule.cron is not None else None,
        jitter=timedelta(seconds=schedule.jitter_seconds) if schedule.jitter_seconds > 0 else None,
    )


def _raise_unless_missing(error: RPCError, operation: str, temporal_id: str) -> None:
    """Treat a missing Temporal Schedule as done; translate every other failure."""
    if error.status is not RPCStatusCode.NOT_FOUND:
        raise _unavailable(operation, temporal_id) from error


def _unavailable(operation: str, temporal_id: str) -> ScheduleTimerUnavailableError:
    return ScheduleTimerUnavailableError(f"could not {operation} Temporal timer {temporal_id!r}")
