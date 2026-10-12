from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, TypeVar

from temporalio.api.common.v1 import Payload
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleBackfill,
    ScheduleDescription,
    ScheduleHandle,
    ScheduleIntervalSpec,
    ScheduleListDescription,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.errors import WorkflowOperationError, WorkflowSubmissionError
from harborrag_runtime.scheduling.errors import (
    DuplicateScheduleError,
    ScheduleNotFoundError,
    ScheduleValidationError,
)
from harborrag_runtime.scheduling.models import (
    ScheduleDefinition,
    ScheduleOverlap,
    ScheduleRun,
    ScheduleView,
)
from harborrag_runtime.scheduling.validation import workflow_policy

from .schemas import ScheduledSourceIngestionInput

T = TypeVar("T")
logger = logging.getLogger("harborrag.runtime.temporal.schedules")
_LIST_CONCURRENCY = 10


# Schedule memo is immutable after creation, so it only marks ownership; the
# action memo is replaced on every update and carries the full definition.
_MEMO_KEY = "harborrag_schedule"


class TemporalScheduleBackend:
    """Run HarborRAG schedule definitions as Temporal Schedules via the SDK."""

    def __init__(self, client: Client, config: TemporalRuntimeConfig) -> None:
        self._client = client
        self._config = config

    async def create(self, definition: ScheduleDefinition) -> None:
        schedule = replace(
            self._schedule(definition, desired_paused=definition.paused),
            state=ScheduleState(note=definition.note, paused=definition.paused),
        )
        try:
            await self._client.create_schedule(
                definition.schedule_id,
                schedule,
                memo={
                    _MEMO_KEY: {
                        "owner": definition.owner.value,
                        "tenant_id": definition.target.tenant_id,
                    }
                },
            )
        except ScheduleAlreadyRunningError:
            # IDs are global across tenants: say only that the ID is taken.
            raise DuplicateScheduleError(
                f"schedule ID {definition.schedule_id!r} is unavailable; choose another ID"
            ) from None
        except RPCError as error:
            if error.status is RPCStatusCode.INVALID_ARGUMENT:
                raise ScheduleValidationError(
                    f"Temporal rejected schedule {definition.schedule_id!r}: {error.message}"
                ) from None
            raise WorkflowSubmissionError(
                f"Could not create schedule {definition.schedule_id!r}"
            ) from error

    async def update(self, definition: ScheduleDefinition, *, desired_paused: bool) -> None:
        schedule = self._schedule(definition, desired_paused=desired_paused)

        # Pause state and note are left as they are; only pause/unpause change them.
        def updater(update: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(
                schedule=replace(
                    update.description.schedule,
                    action=schedule.action,
                    spec=schedule.spec,
                    policy=schedule.policy,
                )
            )

        await self._update(definition.schedule_id, updater)

    async def describe(self, schedule_id: str) -> ScheduleView:
        description = await self._operate(
            schedule_id, "describe", self._handle(schedule_id).describe()
        )
        view = await _view(description)
        if view is None:
            raise ScheduleNotFoundError(f"schedule {schedule_id!r} was not found")
        return view

    async def list_all(self) -> list[ScheduleView]:
        """Every HarborRAG schedule; unreadable definitions are logged and skipped."""

        try:
            entries = [entry async for entry in await self._client.list_schedules()]
        except RPCError as error:
            raise WorkflowOperationError("Could not list schedules") from error
        views: list[ScheduleView] = []

        async def describe_entry(entry: ScheduleListDescription) -> ScheduleView | None:
            if _MEMO_KEY not in await entry.memo():
                return None
            try:
                return await self.describe(entry.id)
            except ScheduleNotFoundError:
                return None

        for offset in range(0, len(entries), _LIST_CONCURRENCY):
            batch = await asyncio.gather(
                *(describe_entry(entry) for entry in entries[offset : offset + _LIST_CONCURRENCY]),
                return_exceptions=True,
            )
            for item in batch:
                if isinstance(item, BaseException):
                    raise item
                if item is not None:
                    views.append(item)
        return views

    async def pause(self, schedule_id: str, *, note: str | None) -> None:
        # Record the intent first: if pausing then fails, expected-paused without
        # paused never raises the unexpected-pause alert.
        await self._set_desired_paused(schedule_id, True)
        await self._operate(schedule_id, "pause", self._handle(schedule_id).pause(note=note))

    async def unpause(self, schedule_id: str, *, note: str | None) -> None:
        await self._operate(schedule_id, "unpause", self._handle(schedule_id).unpause(note=note))
        await self._set_desired_paused(schedule_id, False)

    async def _set_desired_paused(self, schedule_id: str, desired_paused: bool) -> None:
        async def updater(update: ScheduleUpdateInput) -> ScheduleUpdate | None:
            description = update.description
            memo = await _memo(description)
            action = description.schedule.action
            if memo is None or not isinstance(action, ScheduleActionStartWorkflow):
                raise ScheduleNotFoundError(f"schedule {schedule_id!r} was not found")
            if memo.get("desired_paused") == desired_paused:
                return None
            return ScheduleUpdate(
                schedule=replace(
                    description.schedule,
                    action=replace(
                        action,
                        memo={_MEMO_KEY: {**memo, "desired_paused": desired_paused}},
                    ),
                )
            )

        await self._update(schedule_id, updater)

    async def trigger(self, schedule_id: str) -> None:
        await self._operate(schedule_id, "trigger", self._handle(schedule_id).trigger())

    async def delete(self, schedule_id: str) -> None:
        await self._operate(schedule_id, "delete", self._handle(schedule_id).delete())

    async def backfill(self, schedule_id: str, *, start_at: datetime, end_at: datetime) -> None:
        await self._operate(
            schedule_id,
            "backfill",
            self._handle(schedule_id).backfill(ScheduleBackfill(start_at=start_at, end_at=end_at)),
        )

    def _handle(self, schedule_id: str) -> ScheduleHandle:
        return self._client.get_schedule_handle(schedule_id)

    def _schedule(self, definition: ScheduleDefinition, *, desired_paused: bool) -> Schedule:
        target = definition.target
        action = ScheduleActionStartWorkflow(
            workflow_policy(definition.workflow).temporal_workflow,
            ScheduledSourceIngestionInput(
                schedule_id=definition.schedule_id,
                tenant_id=target.tenant_id,
                connection_id=target.connection_id,
                source_scope_id=target.source_scope_id,
                force_reprocess=target.force_reprocess,
            ),
            id=f"harborrag-scheduled-source:{definition.schedule_id}",
            task_queue=self._config.task_queues.discovery,
            execution_timeout=timedelta(seconds=self._config.workflow_execution_timeout_seconds),
            task_timeout=timedelta(seconds=self._config.workflow_task_timeout_seconds),
            memo={_MEMO_KEY: definition.to_memo(desired_paused=desired_paused)},
        )
        return Schedule(
            action=action,
            spec=ScheduleSpec(
                cron_expressions=[definition.cron] if definition.cron is not None else [],
                intervals=(
                    [ScheduleIntervalSpec(every=timedelta(seconds=definition.interval_seconds))]
                    if definition.interval_seconds is not None
                    else []
                ),
                time_zone_name=definition.timezone,
                jitter=(
                    timedelta(seconds=definition.jitter_seconds)
                    if definition.jitter_seconds is not None
                    else None
                ),
            ),
            policy=SchedulePolicy(
                overlap=_OVERLAP[definition.overlap],
                catchup_window=timedelta(seconds=definition.catchup_window_seconds),
                pause_on_failure=definition.pause_on_failure,
            ),
        )

    async def _update(
        self,
        schedule_id: str,
        updater: Callable[[ScheduleUpdateInput], Awaitable[ScheduleUpdate | None]]
        | Callable[[ScheduleUpdateInput], ScheduleUpdate | None],
    ) -> None:
        """Read-modify-write a schedule, retrying when another writer got there first.

        API replicas sync the same schedules at startup; the updater re-reads the
        current description on each attempt, so a retry never loses their change.
        """

        for attempt in range(1, _UPDATE_ATTEMPTS + 1):
            try:
                await self._handle(schedule_id).update(updater)
                return
            except RPCError as error:
                if error.status not in _CONFLICT_STATUSES or attempt == _UPDATE_ATTEMPTS:
                    raise _schedule_error(schedule_id, "update", error) from error
                logger.info(
                    "Schedule update conflicted; retrying schedule_id=%s attempt=%d",
                    schedule_id,
                    attempt,
                )

    @staticmethod
    async def _operate(schedule_id: str, operation: str, awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except RPCError as error:
            raise _schedule_error(schedule_id, operation, error) from error


def _schedule_error(schedule_id: str, operation: str, error: RPCError) -> Exception:
    if error.status is RPCStatusCode.NOT_FOUND:
        return ScheduleNotFoundError(f"schedule {schedule_id!r} was not found")
    if error.status is RPCStatusCode.INVALID_ARGUMENT:
        return ScheduleValidationError(
            f"Temporal rejected {operation} of schedule {schedule_id!r}: {error.message}"
        )
    return WorkflowOperationError(f"Could not {operation} schedule {schedule_id!r}")


_UPDATE_ATTEMPTS = 3
# Status Temporal returns when an update's conflict token is stale.
_CONFLICT_STATUSES = frozenset({RPCStatusCode.FAILED_PRECONDITION, RPCStatusCode.ABORTED})


_OVERLAP = {
    ScheduleOverlap.SKIP: ScheduleOverlapPolicy.SKIP,
    ScheduleOverlap.BUFFER_ONE: ScheduleOverlapPolicy.BUFFER_ONE,
    ScheduleOverlap.BUFFER_ALL: ScheduleOverlapPolicy.BUFFER_ALL,
    ScheduleOverlap.CANCEL_OTHER: ScheduleOverlapPolicy.CANCEL_OTHER,
    ScheduleOverlap.TERMINATE_OTHER: ScheduleOverlapPolicy.TERMINATE_OTHER,
    ScheduleOverlap.ALLOW_ALL: ScheduleOverlapPolicy.ALLOW_ALL,
}


async def _memo(description: ScheduleDescription) -> dict[str, Any] | None:
    memo = getattr(description.schedule.action, "memo", None) or {}
    payload = memo.get(_MEMO_KEY)
    if payload is None:
        return None
    if isinstance(payload, Payload):
        (payload,) = await description.data_converter.decode([payload])
    return payload if isinstance(payload, dict) else None


async def _view(description: ScheduleDescription) -> ScheduleView | None:
    """Rebuild the definition from the action memo; None for schedules HarborRAG does not own."""

    schedule = description.schedule
    payload = await _memo(description)
    if payload is None:
        return None
    try:
        definition = ScheduleDefinition.from_memo(
            payload,
            paused=schedule.state.paused,
            note=schedule.state.note,
        )
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "Ignoring schedule with an unreadable definition schedule_id=%s", description.id
        )
        return None
    info = description.info
    return ScheduleView(
        definition=definition,
        next_action_times=tuple(info.next_action_times),
        recent_actions=tuple(
            ScheduleRun(
                scheduled_at=action.scheduled_at,
                started_at=action.started_at,
                workflow_id=getattr(action.action, "workflow_id", None),
            )
            for action in info.recent_actions
        ),
        num_actions=info.num_actions,
        num_actions_skipped_overlap=info.num_actions_skipped_overlap,
        desired_paused=bool(payload.get("desired_paused", False)),
        declared_paused=(
            bool(payload["declared_paused"]) if "declared_paused" in payload else None
        ),
        created_at=info.created_at,
        updated_at=info.last_updated_at,
    )
