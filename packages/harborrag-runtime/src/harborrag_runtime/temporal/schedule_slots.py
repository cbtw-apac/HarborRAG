"""The workflow that runs one ``/v1/admin/schedules`` slot, and the API-side worker for it.

The timer starts ``harborrag.schedule_slot`` for every slot; its activities run inside
the API process, which composes the schedule use cases. The stored schedule decides
what each slot does. Workflow code stays deterministic, and the payloads here are
recorded in history: new fields need defaults.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ActivityError
from temporalio.worker import Worker

from harborrag_runtime.application.control_plane.schedule_models import ScheduleKey, SlotAttempt
from harborrag_runtime.config.temporal import TemporalRuntimeConfig

from .connection import connect_temporal_client
from .failure_handling import durable_failure
from .sandbox import workflow_sandbox_runner

if TYPE_CHECKING:
    from collections.abc import Callable

    from temporalio.client import Client

    from harborrag_runtime.application.control_plane.schedules import ConnectionScheduleService
    from harborrag_runtime.config.settings import RuntimeSettings

SLOT_WORKFLOW: Final = "harborrag.schedule_slot"
FIRST_POLL: Final = timedelta(seconds=30)
LONGEST_POLL: Final = timedelta(minutes=5)
LONGEST_WAIT: Final = timedelta(hours=24)
LONGEST_FOLLOW: Final = timedelta(days=7)
POLLS_PER_RUN: Final = 200
_ACTIVITY_TIMEOUT: Final = timedelta(seconds=60)
_START_ONCE: Final = RetryPolicy(maximum_attempts=1)
_RETRY_BRIEFLY: Final = RetryPolicy(maximum_attempts=3)
_SCHEDULED_START: Final = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")


@dataclass(frozen=True, slots=True)
class ScheduleSlotInput:
    """Which schedule a slot belongs to, and the run it follows, if any.

    Args:
        tenant_id: Owning tenant.
        schedule_id: The schedule, unique in the tenant.
        ingestion_id: A run to follow instead of starting one; set for a triggered
            run and when a followed run continues as new.
        follow_until: When following the run stops; carried across continue-as-new.
    """

    tenant_id: str
    schedule_id: str
    ingestion_id: str | None = None
    follow_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class SlotRunInput:
    """One attempt to start a slot's run.

    Args:
        tenant_id: Owning tenant.
        schedule_id: The schedule whose slot came due.
        due_at: When the slot came due.
        attempt: ``due`` when it came due, ``after_wait`` once the run it waited for ended.
    """

    tenant_id: str
    schedule_id: str
    due_at: datetime
    attempt: str = SlotAttempt.DUE.value


@dataclass(frozen=True, slots=True)
class SlotRunResult:
    """What a slot attempt did.

    Args:
        outcome: ``started``, ``skipped``, ``queued`` or ``ignored``.
        ingestion_id: The run it started, or the run it skipped or waits behind.
    """

    outcome: str
    ingestion_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduleRunInput:
    """A run a schedule started, to check or count.

    Args:
        tenant_id: Owning tenant.
        schedule_id: The schedule that started the run.
        ingestion_id: The run.
    """

    tenant_id: str
    schedule_id: str
    ingestion_id: str


@workflow.defn(name=SLOT_WORKFLOW)
class ScheduleSlotWorkflow:
    """Start one slot's run, waiting for the connection when the schedule queues, then follow it.

    Following ends by counting the run toward the schedule's auto-pause.
    """

    @workflow.run
    async def run(self, request: ScheduleSlotInput) -> None:
        """Run the slot, or follow ``request.ingestion_id`` when it is set."""
        ingestion_id = request.ingestion_id or await _start_slot(request)
        if ingestion_id is not None:
            await _follow(request, ingestion_id)


async def _start_slot(request: ScheduleSlotInput) -> str | None:
    """Run the due slot and return the run it started, waiting for its turn when queued."""
    due_at = _due_at()
    start = await _run_slot(request, due_at, SlotAttempt.DUE)
    if start.outcome == "queued":
        return await _wait_for_turn(request, due_at, start.ingestion_id)
    await _refresh_next_run(request)
    return start.ingestion_id if start.outcome == "started" else None


def _due_at() -> datetime:
    """The slot's nominal time from the schedule, or the workflow's start time."""
    info = workflow.info()
    return info.typed_search_attributes.get(_SCHEDULED_START) or info.workflow_start_time


async def _wait_for_turn(
    request: ScheduleSlotInput, due_at: datetime, blocking: str | None
) -> str | None:
    """Wait for the blocking run to end, then start; give the waiting place back on any exit.

    A slot that waits holds the schedule's one waiting place. Giving up after
    ``LONGEST_WAIT``, failing or being cancelled releases it, so a later slot may wait.
    """
    try:
        await _refresh_next_run(request)
        start = await _wait_then_start(request, due_at, blocking)
    except (ActivityError, asyncio.CancelledError):
        await _release_waiting_slot(request)
        raise
    if start is None:
        workflow.logger.warning(
            "Schedule slot gave up waiting for the connection schedule_id=%s",
            request.schedule_id,
        )
        await _release_waiting_slot(request)
        return None
    return start.ingestion_id if start.outcome == "started" else None


async def _wait_then_start(
    request: ScheduleSlotInput, due_at: datetime, blocking: str | None
) -> SlotRunResult | None:
    """Poll until the blocking run ends and the slot gets past ``queued``; ``None`` on timeout."""
    deadline = workflow.now() + LONGEST_WAIT
    delay = FIRST_POLL
    while workflow.now() < deadline:
        await workflow.sleep(delay)
        delay = min(delay * 2, LONGEST_POLL)
        if blocking is not None and not await _run_ended(request, blocking):
            continue
        start = await _run_slot(request, due_at, SlotAttempt.AFTER_WAIT)
        if start.outcome != "queued":
            return start
        blocking = start.ingestion_id
    return None


async def _follow(request: ScheduleSlotInput, ingestion_id: str) -> None:
    """Poll the run until it ends, then count it; continue as new to keep history short."""
    follow_until = request.follow_until or workflow.now() + LONGEST_FOLLOW
    delay = FIRST_POLL
    for _ in range(POLLS_PER_RUN):
        if workflow.now() >= follow_until:
            workflow.logger.warning(
                "Stopped following a scheduled run that did not end schedule_id=%s ingestion_id=%s",
                request.schedule_id,
                ingestion_id,
            )
            return
        await workflow.sleep(delay)
        delay = min(delay * 2, LONGEST_POLL)
        if await _run_ended(request, ingestion_id):
            await workflow.execute_activity(
                "harborrag.finish_schedule_run",
                _run_input(request, ingestion_id),
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY_BRIEFLY,
            )
            return
    workflow.continue_as_new(replace(request, ingestion_id=ingestion_id, follow_until=follow_until))


async def _run_slot(
    request: ScheduleSlotInput, due_at: datetime, attempt: SlotAttempt
) -> SlotRunResult:
    """Start the slot's run once: a retried start would find its own run and record a skip."""
    result: SlotRunResult = await workflow.execute_activity(
        "harborrag.run_schedule_slot",
        SlotRunInput(
            tenant_id=request.tenant_id,
            schedule_id=request.schedule_id,
            due_at=due_at,
            attempt=attempt.value,
        ),
        result_type=SlotRunResult,
        start_to_close_timeout=_ACTIVITY_TIMEOUT,
        retry_policy=_START_ONCE,
    )
    return result


async def _run_ended(request: ScheduleSlotInput, ingestion_id: str) -> bool:
    ended: bool = await workflow.execute_activity(
        "harborrag.schedule_run_ended",
        _run_input(request, ingestion_id),
        result_type=bool,
        start_to_close_timeout=_ACTIVITY_TIMEOUT,
        retry_policy=_RETRY_BRIEFLY,
    )
    return ended


async def _refresh_next_run(request: ScheduleSlotInput) -> None:
    """Store the schedule's next slot time; a failure only leaves the stored time stale."""
    try:
        await workflow.execute_activity(
            "harborrag.refresh_schedule_next_run",
            request,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY_BRIEFLY,
        )
    except ActivityError as error:
        workflow.logger.warning(
            "Could not refresh the next slot time schedule_id=%s error_type=%s",
            request.schedule_id,
            durable_failure(error)[0],
        )


async def _release_waiting_slot(request: ScheduleSlotInput) -> None:
    """Give the schedule's waiting place back; a failure is logged, not raised."""
    try:
        await workflow.execute_activity(
            "harborrag.release_waiting_slot",
            request,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY_BRIEFLY,
        )
    except ActivityError as error:
        workflow.logger.error(
            "Could not release the waiting slot schedule_id=%s error_type=%s",
            request.schedule_id,
            durable_failure(error)[0],
        )


def _run_input(request: ScheduleSlotInput, ingestion_id: str) -> ScheduleRunInput:
    return ScheduleRunInput(
        tenant_id=request.tenant_id, schedule_id=request.schedule_id, ingestion_id=ingestion_id
    )


class ScheduleSlotActivities:
    """The slot workflow's activities, run in the API process against the schedule use cases.

    Args:
        schedules: The schedule use cases the API composes.
    """

    def __init__(self, schedules: ConnectionScheduleService) -> None:
        self._schedules = schedules

    @activity.defn(name="harborrag.run_schedule_slot")
    async def run_schedule_slot(self, request: SlotRunInput) -> SlotRunResult:
        """Start the slot's run, or record why it did not start."""
        start = await self._schedules.run_slot(
            ScheduleKey(request.tenant_id, request.schedule_id),
            due_at=request.due_at,
            attempt=SlotAttempt(request.attempt),
        )
        return SlotRunResult(outcome=start.outcome, ingestion_id=start.ingestion_id)

    @activity.defn(name="harborrag.refresh_schedule_next_run")
    async def refresh_schedule_next_run(self, request: ScheduleSlotInput) -> None:
        """Store when the schedule's next slot fires."""
        await self._schedules.refresh_next_run(ScheduleKey(request.tenant_id, request.schedule_id))

    @activity.defn(name="harborrag.schedule_run_ended")
    async def schedule_run_ended(self, request: ScheduleRunInput) -> bool:
        """Return whether the run has ended."""
        return await self._schedules.run_ended(
            ScheduleKey(request.tenant_id, request.schedule_id), request.ingestion_id
        )

    @activity.defn(name="harborrag.finish_schedule_run")
    async def finish_schedule_run(self, request: ScheduleRunInput) -> None:
        """Count the ended run toward the schedule's auto-pause."""
        await self._schedules.finish_run(
            ScheduleKey(request.tenant_id, request.schedule_id), request.ingestion_id
        )

    @activity.defn(name="harborrag.release_waiting_slot")
    async def release_waiting_slot(self, request: ScheduleSlotInput) -> None:
        """Let a later slot of the schedule wait."""
        await self._schedules.release_waiting_slot(
            ScheduleKey(request.tenant_id, request.schedule_id)
        )

    def all(self) -> list[Callable[..., Any]]:
        """Every activity, for the worker."""
        return [
            self.run_schedule_slot,
            self.refresh_schedule_next_run,
            self.schedule_run_ended,
            self.finish_schedule_run,
            self.release_waiting_slot,
        ]


async def serve_schedule_slots(
    settings: RuntimeSettings, schedules: ConnectionScheduleService
) -> None:
    """Connect to Temporal and run the slot worker until cancelled.

    Args:
        settings: Runtime settings; ``schedule_task_queue`` names the queue.
        schedules: The schedule use cases the activities call.

    Raises:
        RuntimeConnectionError: Temporal cannot be reached.
    """
    config = TemporalRuntimeConfig.from_settings(settings)
    client = await connect_temporal_client(config)
    await run_schedule_slot_worker(
        client, schedules, task_queue=settings.schedule_task_queue, config=config
    )


async def run_schedule_slot_worker(
    client: Client,
    schedules: ConnectionScheduleService,
    *,
    task_queue: str,
    config: TemporalRuntimeConfig,
) -> None:
    """Poll ``task_queue`` for slot workflows and activities until cancelled.

    Cancelling shuts the worker down, letting running activities finish within the
    configured graceful shutdown time.

    Args:
        client: Connected Temporal client.
        schedules: The schedule use cases the activities call.
        task_queue: Queue the timers start slot workflows on.
        config: Temporal settings that name the worker and its shutdown time.
    """
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ScheduleSlotWorkflow],
        activities=ScheduleSlotActivities(schedules).all(),
        workflow_runner=workflow_sandbox_runner([ScheduleSlotWorkflow]),
        identity=config.worker.identity,
        graceful_shutdown_timeout=timedelta(seconds=config.worker.graceful_shutdown_seconds),
    )
    await worker.run()
