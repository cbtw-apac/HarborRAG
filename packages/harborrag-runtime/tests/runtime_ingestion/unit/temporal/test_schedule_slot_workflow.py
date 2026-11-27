"""The schedule slot workflow on the Temporal test server, run by the API's slot worker."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
import pytest_asyncio
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment

from harborrag_runtime.application.control_plane.schedule_models import (
    ScheduleKey,
    SlotAttempt,
    SlotStart,
)
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal.schedule_slots import (
    LONGEST_WAIT,
    POLLS_PER_RUN,
    ScheduleSlotInput,
    ScheduleSlotWorkflow,
    run_schedule_slot_worker,
)

pytestmark = [pytest.mark.graybox, pytest.mark.asyncio]

QUEUE = "harborrag-schedules-test"


@dataclass
class _Schedules:
    """Schedule use cases the slot activities call, scripted per test.

    Args:
        starts: What each ``run_slot`` call returns, in order; an exception is raised.
        polls_until_ended: ``run_ended`` checks of a run before it reports the run ended.
        refresh_fails: Make ``refresh_next_run`` fail.
    """

    starts: list[SlotStart | Exception] = field(default_factory=list)
    polls_until_ended: dict[str, int] = field(default_factory=dict)
    refresh_fails: bool = False
    attempts: list[SlotAttempt] = field(default_factory=list)
    due_at: list[datetime] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    refreshed: int = 0
    released: int = 0

    async def run_slot(
        self, key: ScheduleKey, *, due_at: datetime, attempt: SlotAttempt
    ) -> SlotStart:
        assert key == ScheduleKey("acme", "nightly")
        self.attempts.append(attempt)
        self.due_at.append(due_at)
        start = self.starts.pop(0)
        if isinstance(start, Exception):
            raise start
        return start

    async def refresh_next_run(self, key: ScheduleKey) -> None:
        self.refreshed += 1
        if self.refresh_fails:
            raise RuntimeError("database down")

    async def run_ended(self, key: ScheduleKey, ingestion_id: str) -> bool:
        self.checked.append(ingestion_id)
        return self.checked.count(ingestion_id) >= self.polls_until_ended.get(ingestion_id, 1)

    async def finish_run(self, key: ScheduleKey, ingestion_id: str) -> None:
        self.finished.append(ingestion_id)

    async def release_waiting_slot(self, key: ScheduleKey) -> None:
        self.released += 1


@pytest_asyncio.fixture
async def environment() -> AsyncIterator[WorkflowEnvironment]:
    try:
        env = await WorkflowEnvironment.start_time_skipping()
    except Exception as error:  # pragma: no cover - depends on the test-server download
        pytest.skip(f"Temporal test server unavailable: {error}")
    try:
        yield env
    finally:
        await env.shutdown()


async def _run(
    environment: WorkflowEnvironment, schedules: _Schedules, request: ScheduleSlotInput
) -> BaseException | None:
    """Run one slot workflow on the production slot worker, then stop the worker."""
    worker = asyncio.create_task(
        run_schedule_slot_worker(
            environment.client,
            schedules,
            task_queue=QUEUE,
            config=TemporalRuntimeConfig(),
        )
    )
    try:
        await environment.client.execute_workflow(
            ScheduleSlotWorkflow.run,
            request,
            id=f"harborrag-schedule-slot:acme:nightly-{uuid.uuid4()}",
            task_queue=QUEUE,
        )
    except WorkflowFailureError as error:
        return error
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    return None


SLOT = ScheduleSlotInput(tenant_id="acme", schedule_id="nightly")


async def test_a_started_slot_follows_its_run_and_counts_it_once(
    environment: WorkflowEnvironment,
) -> None:
    schedules = _Schedules(starts=[SlotStart("started", "ing_1")], polls_until_ended={"ing_1": 3})

    assert await _run(environment, schedules, SLOT) is None

    assert schedules.attempts == [SlotAttempt.DUE]
    assert schedules.checked == ["ing_1", "ing_1", "ing_1"]
    assert schedules.finished == ["ing_1"]
    assert (schedules.refreshed, schedules.released) == (1, 0)


async def test_a_queued_slot_starts_after_the_blocking_run_ends(
    environment: WorkflowEnvironment,
) -> None:
    schedules = _Schedules(
        starts=[
            SlotStart("queued", "ing_busy"),
            SlotStart("queued", "ing_next"),
            SlotStart("started", "ing_3"),
        ],
        polls_until_ended={"ing_busy": 2},
    )

    assert await _run(environment, schedules, SLOT) is None

    assert schedules.attempts == [SlotAttempt.DUE, SlotAttempt.AFTER_WAIT, SlotAttempt.AFTER_WAIT]
    assert len(set(schedules.due_at)) == 1
    assert schedules.checked == ["ing_busy", "ing_busy", "ing_next", "ing_3"]
    assert schedules.finished == ["ing_3"]
    assert (schedules.refreshed, schedules.released) == (1, 0)


async def test_a_waiting_slot_gives_its_place_back_after_a_day(
    environment: WorkflowEnvironment,
) -> None:
    schedules = _Schedules(
        starts=[SlotStart("queued", "ing_busy")], polls_until_ended={"ing_busy": 10_000}
    )

    assert await _run(environment, schedules, SLOT) is None

    assert schedules.attempts == [SlotAttempt.DUE]
    assert schedules.released == 1 and schedules.finished == []
    assert len(schedules.checked) < LONGEST_WAIT.total_seconds() / 300 + 5


async def test_a_waiting_slot_gives_its_place_back_when_the_start_fails(
    environment: WorkflowEnvironment,
) -> None:
    schedules = _Schedules(starts=[SlotStart("queued", "ing_busy"), RuntimeError("database down")])

    assert isinstance(await _run(environment, schedules, SLOT), WorkflowFailureError)

    assert schedules.attempts == [SlotAttempt.DUE, SlotAttempt.AFTER_WAIT]
    assert schedules.released == 1


async def test_a_failed_due_start_fails_once_without_releasing(
    environment: WorkflowEnvironment,
) -> None:
    schedules = _Schedules(starts=[RuntimeError("database down")])

    assert isinstance(await _run(environment, schedules, SLOT), WorkflowFailureError)

    assert schedules.attempts == [SlotAttempt.DUE]
    assert schedules.released == 0


@pytest.mark.parametrize("outcome", ["skipped", "ignored"])
async def test_a_slot_that_did_not_start_ends(
    environment: WorkflowEnvironment, outcome: Any
) -> None:
    schedules = _Schedules(starts=[SlotStart(outcome, "ing_busy")], refresh_fails=True)

    assert await _run(environment, schedules, SLOT) is None

    assert schedules.checked == [] and schedules.finished == []
    assert schedules.refreshed == 3


async def test_a_triggered_run_is_followed_without_a_slot(environment: WorkflowEnvironment) -> None:
    schedules = _Schedules(polls_until_ended={"ing_manual": 2})

    assert (
        await _run(environment, schedules, ScheduleSlotInput("acme", "nightly", "ing_manual"))
        is None
    )

    assert schedules.attempts == [] and schedules.refreshed == 0
    assert schedules.finished == ["ing_manual"]


async def test_a_long_run_is_followed_across_continue_as_new(
    environment: WorkflowEnvironment,
) -> None:
    polls = POLLS_PER_RUN + 3
    schedules = _Schedules(polls_until_ended={"ing_long": polls})

    assert (
        await _run(environment, schedules, ScheduleSlotInput("acme", "nightly", "ing_long")) is None
    )

    assert len(schedules.checked) == polls
    assert schedules.finished == ["ing_long"]
