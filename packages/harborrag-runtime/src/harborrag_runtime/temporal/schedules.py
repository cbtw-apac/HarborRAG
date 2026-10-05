from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TypeVar

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
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission
from harborrag_runtime.scheduling.errors import DuplicateScheduleError, ScheduleNotFoundError
from harborrag_runtime.scheduling.models import (
    ScheduleDefinition,
    ScheduleOverlap,
    ScheduleRun,
    ScheduleView,
)
from harborrag_runtime.scheduling.validation import workflow_policy

from .gateway import to_temporal_source
from .schemas import ScheduledSourceIngestionInput, SourceIngestionInput

T = TypeVar("T")


class IngestionScheduleClient:
    """Manage Temporal Schedules that start recurring source ingestions."""

    def __init__(self, client: Client, config: TemporalRuntimeConfig) -> None:
        self._client = client
        self._config = config

    async def upsert_source_ingestion_schedule(
        self,
        schedule_id: str,
        cron_expression: str,
        request: SourceIngestionInput,
    ) -> None:
        if not schedule_id.strip() or schedule_id != schedule_id.strip():
            raise ValueError("schedule_id must be non-empty and have no outer whitespace")
        if not cron_expression.strip() or cron_expression != cron_expression.strip():
            raise ValueError("cron_expression must be non-empty and have no outer whitespace")

        request = replace(request, workflow_options=self._config.workflow_options())
        schedule = self._schedule(schedule_id, cron_expression, request)
        try:
            await self._client.create_schedule(schedule_id, schedule)
        except ScheduleAlreadyRunningError:
            handle = self._client.get_schedule_handle(schedule_id)
            try:
                await handle.update(
                    lambda update: ScheduleUpdate(
                        schedule=replace(
                            update.description.schedule,
                            action=schedule.action,
                            spec=schedule.spec,
                            policy=schedule.policy,
                        )
                    )
                )
            except RPCError as error:
                raise WorkflowOperationError(
                    f"Could not update ingestion schedule {schedule_id!r}"
                ) from error
        except RPCError as error:
            raise WorkflowSubmissionError(
                f"Could not create ingestion schedule {schedule_id!r}"
            ) from error

    async def pause(self, schedule_id: str, *, note: str | None = None) -> None:
        await self._operate(
            schedule_id,
            "pause",
            self._client.get_schedule_handle(schedule_id).pause(note=note),
        )

    async def unpause(self, schedule_id: str, *, note: str | None = None) -> None:
        await self._operate(
            schedule_id,
            "unpause",
            self._client.get_schedule_handle(schedule_id).unpause(note=note),
        )

    async def trigger(self, schedule_id: str) -> None:
        await self._operate(
            schedule_id,
            "trigger",
            self._client.get_schedule_handle(schedule_id).trigger(
                overlap=ScheduleOverlapPolicy.SKIP
            ),
        )

    async def delete(self, schedule_id: str) -> None:
        await self._operate(
            schedule_id,
            "delete",
            self._client.get_schedule_handle(schedule_id).delete(),
        )

    def _schedule(
        self,
        schedule_id: str,
        cron_expression: str,
        request: SourceIngestionInput,
    ) -> Schedule:
        source = ScheduledSourceIngestionInput(schedule_id=schedule_id, source=request)
        action = ScheduleActionStartWorkflow(
            "harborrag.scheduled_source_ingestion",
            source,
            id=f"harborrag-scheduled-source:{schedule_id}",
            task_queue=self._config.task_queues.discovery,
            execution_timeout=timedelta(
                seconds=self._config.workflow_execution_timeout_seconds
            ),
            task_timeout=timedelta(seconds=self._config.workflow_task_timeout_seconds),
        )
        return Schedule(
            action=action,
            spec=ScheduleSpec(cron_expressions=[cron_expression]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
        )

    @staticmethod
    async def _operate(
        schedule_id: str,
        operation: str,
        awaitable: Awaitable[object],
    ) -> None:
        try:
            await awaitable
        except RPCError as error:
            raise WorkflowOperationError(
                f"Could not {operation} ingestion schedule {schedule_id!r}"
            ) from error


# Schedule memo is immutable after creation, so it only marks ownership; the
# action memo is replaced on every update and carries the full definition.
_MEMO_KEY = "harborrag_schedule"


class TemporalScheduleBackend:
    """Run HarborRAG schedule definitions as Temporal Schedules via the SDK."""

    def __init__(self, client: Client, config: TemporalRuntimeConfig) -> None:
        self._client = client
        self._config = config

    async def create(self, definition: ScheduleDefinition, source: PreparedSourceSubmission) -> None:
        schedule = replace(
            self._schedule(definition, source),
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
            raise DuplicateScheduleError(
                f"schedule ID {definition.schedule_id!r} already exists; choose a new ID "
                "or update the existing schedule"
            ) from None
        except RPCError as error:
            raise WorkflowSubmissionError(
                f"Could not create schedule {definition.schedule_id!r}"
            ) from error

    async def update(self, definition: ScheduleDefinition, source: PreparedSourceSubmission) -> None:
        schedule = self._schedule(definition, source)

        def updater(update: ScheduleUpdateInput) -> ScheduleUpdate:
            return ScheduleUpdate(
                schedule=replace(
                    update.description.schedule,
                    action=schedule.action,
                    spec=schedule.spec,
                    policy=schedule.policy,
                    state=replace(
                        update.description.schedule.state,
                        note=definition.note,
                        paused=definition.paused,
                    ),
                )
            )

        await self._operate(definition.schedule_id, "update", self._handle(definition.schedule_id).update(updater))

    async def describe(self, schedule_id: str) -> ScheduleView:
        description = await self._operate(
            schedule_id, "describe", self._handle(schedule_id).describe()
        )
        view = await _view(description)
        if view is None:
            raise ScheduleNotFoundError(f"schedule {schedule_id!r} was not found")
        return view

    async def list_all(self) -> list[ScheduleView]:
        try:
            entries = [entry async for entry in await self._client.list_schedules()]
        except RPCError as error:
            raise WorkflowOperationError("Could not list schedules") from error
        views: list[ScheduleView] = []
        for entry in entries:
            if _MEMO_KEY not in await entry.memo():
                continue
            try:
                views.append(await self.describe(entry.id))
            except ScheduleNotFoundError:
                continue
        return views

    async def pause(self, schedule_id: str, *, note: str | None) -> None:
        await self._operate(schedule_id, "pause", self._handle(schedule_id).pause(note=note))

    async def unpause(self, schedule_id: str, *, note: str | None) -> None:
        await self._operate(schedule_id, "unpause", self._handle(schedule_id).unpause(note=note))

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

    def _schedule(self, definition: ScheduleDefinition, source: PreparedSourceSubmission) -> Schedule:
        request = replace(to_temporal_source(source), workflow_options=self._config.workflow_options())
        action = ScheduleActionStartWorkflow(
            workflow_policy(definition.workflow).temporal_workflow,
            ScheduledSourceIngestionInput(schedule_id=definition.schedule_id, source=request),
            id=f"harborrag-scheduled-source:{definition.schedule_id}",
            task_queue=self._config.task_queues.discovery,
            execution_timeout=timedelta(seconds=self._config.workflow_execution_timeout_seconds),
            task_timeout=timedelta(seconds=self._config.workflow_task_timeout_seconds),
            memo={_MEMO_KEY: definition.to_memo()},
        )
        return Schedule(
            action=action,
            spec=ScheduleSpec(
                cron_expressions=[definition.cron] if definition.cron is not None else [],
                intervals=(
                    [
                        ScheduleIntervalSpec(
                            every=timedelta(seconds=definition.interval_seconds)
                        )
                    ]
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

    @staticmethod
    async def _operate(schedule_id: str, operation: str, awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                raise ScheduleNotFoundError(f"schedule {schedule_id!r} was not found") from None
            raise WorkflowOperationError(
                f"Could not {operation} schedule {schedule_id!r}"
            ) from error


_OVERLAP = {
    ScheduleOverlap.SKIP: ScheduleOverlapPolicy.SKIP,
    ScheduleOverlap.BUFFER_ONE: ScheduleOverlapPolicy.BUFFER_ONE,
    ScheduleOverlap.BUFFER_ALL: ScheduleOverlapPolicy.BUFFER_ALL,
    ScheduleOverlap.CANCEL_OTHER: ScheduleOverlapPolicy.CANCEL_OTHER,
    ScheduleOverlap.TERMINATE_OTHER: ScheduleOverlapPolicy.TERMINATE_OTHER,
    ScheduleOverlap.ALLOW_ALL: ScheduleOverlapPolicy.ALLOW_ALL,
}


async def _view(description: ScheduleDescription) -> ScheduleView | None:
    """Rebuild the definition from the action memo; None for schedules HarborRAG does not own."""

    schedule = description.schedule
    memo = getattr(schedule.action, "memo", None) or {}
    payload = memo.get(_MEMO_KEY)
    if payload is None:
        return None
    if isinstance(payload, Payload):
        (payload,) = await description.data_converter.decode([payload])
    definition = ScheduleDefinition.from_memo(
        payload,
        paused=schedule.state.paused,
        note=schedule.state.note,
    )
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
        created_at=info.created_at,
        updated_at=info.last_updated_at,
    )


__all__ = ["IngestionScheduleClient", "TemporalScheduleBackend"]
