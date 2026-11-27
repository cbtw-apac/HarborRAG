"""Temporal timers of ``/v1/admin/schedules`` against a fake Temporal client."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from temporalio.client import (
    Schedule as TemporalSchedule,
)
from temporalio.client import (
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
)
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.domain.schedule import Schedule, SchedulePause
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.temporal import connection as connection_module
from harborrag_runtime.temporal.schedule_slots import ScheduleSlotInput
from harborrag_runtime.temporal.schedule_timers import (
    ScheduleTimerUnavailableError,
    TemporalScheduleTimers,
    connect_schedule_timers,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

NEXT_RUN = datetime(2026, 11, 28, 2, 0, tzinfo=UTC)
TEMPORAL_ID = "harborrag-schedule:acme:nightly"
QUEUE = "harborrag-schedules"


def _missing() -> RPCError:
    return RPCError("not found", RPCStatusCode.NOT_FOUND, b"")


def _down() -> RPCError:
    return RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")


class _Client:
    """Temporal client double with one schedule handle that describes ``paused``."""

    def __init__(self, *, paused: bool = False) -> None:
        self.create_schedule = AsyncMock()
        self.start_workflow = AsyncMock()
        self.handle = SimpleNamespace(
            pause=AsyncMock(),
            unpause=AsyncMock(),
            delete=AsyncMock(),
            update=AsyncMock(),
            describe=AsyncMock(
                return_value=SimpleNamespace(
                    schedule=SimpleNamespace(state=ScheduleState(paused=paused)),
                    info=SimpleNamespace(next_action_times=[NEXT_RUN]),
                )
            ),
        )
        self.get_schedule_handle = Mock(return_value=self.handle)


def _schedule(**changes: Any) -> Schedule:
    values: dict[str, Any] = {
        "tenant_id": "acme",
        "schedule_id": "nightly",
        "project_id": "demo",
        "connection_id": "demo-tickets",
        "interval_seconds": 3600,
    }
    values.update(changes)
    return Schedule(**values)


def _timers(client: _Client) -> TemporalScheduleTimers:
    return TemporalScheduleTimers(client, task_queue=QUEUE)


async def test_put_creates_an_interval_timer_carrying_only_the_schedule_identity() -> None:
    client = _Client()

    next_run_at = await _timers(client).put(
        _schedule(jitter_seconds=60), catch_up_window_seconds=90
    )

    assert next_run_at == NEXT_RUN
    temporal_id, desired = client.create_schedule.await_args.args
    assert temporal_id == TEMPORAL_ID
    assert client.create_schedule.await_args.kwargs["memo"] == {
        "harborrag_api_schedule": {"tenant_id": "acme", "schedule_id": "nightly"}
    }
    assert desired.action.workflow == "harborrag.schedule_slot"
    assert desired.action.args[0] == ScheduleSlotInput(tenant_id="acme", schedule_id="nightly")
    assert desired.action.id == "harborrag-schedule-slot:acme:nightly"
    assert desired.action.task_queue == QUEUE
    assert [spec.every.total_seconds() for spec in desired.spec.intervals] == [3600]
    assert desired.spec.cron_expressions == [] and desired.spec.time_zone_name is None
    assert desired.spec.jitter.total_seconds() == 60
    assert desired.policy.overlap is ScheduleOverlapPolicy.ALLOW_ALL
    assert desired.policy.catchup_window.total_seconds() == 90
    assert desired.state.paused is False and desired.state.note is None
    client.get_schedule_handle.assert_called_with(TEMPORAL_ID)


async def test_put_creates_a_paused_cron_timer_in_its_time_zone() -> None:
    client = _Client(paused=True)
    pause = SchedulePause(reason="maintenance", by="u_ops", at=NEXT_RUN)
    schedule = _schedule(
        interval_seconds=None, cron="0 2 * * *", timezone="Asia/Ho_Chi_Minh", paused=pause
    )

    next_run_at = await _timers(client).put(schedule, catch_up_window_seconds=60)

    assert next_run_at is None
    desired = client.create_schedule.await_args.args[1]
    assert desired.spec.cron_expressions == ["0 2 * * *"] and desired.spec.intervals == []
    assert desired.spec.time_zone_name == "Asia/Ho_Chi_Minh" and desired.spec.jitter is None
    assert (desired.state.paused, desired.state.note) == (True, "maintenance")


async def test_put_replaces_an_existing_timer_in_place() -> None:
    client = _Client()
    client.create_schedule.side_effect = ScheduleAlreadyRunningError()
    existing = TemporalSchedule(
        action=ScheduleActionStartWorkflow("old.workflow", id="old-id", task_queue="old"),
        spec=ScheduleSpec(cron_expressions=["0 1 * * *"]),
        policy=SchedulePolicy(),
        state=ScheduleState(paused=True, note="old pause"),
    )

    await _timers(client).put(_schedule(interval_seconds=7200), catch_up_window_seconds=60)

    updater = client.handle.update.await_args.args[0]
    update = updater(SimpleNamespace(description=SimpleNamespace(schedule=existing)))
    assert update.schedule.action.workflow == "harborrag.schedule_slot"
    assert update.schedule.spec.cron_expressions == []
    assert update.schedule.spec.intervals[0].every.total_seconds() == 7200
    assert update.schedule.policy.overlap is ScheduleOverlapPolicy.ALLOW_ALL
    assert (update.schedule.state.paused, update.schedule.state.note) == (False, None)


async def test_missing_timers_are_no_ops() -> None:
    client = _Client()
    for method in ("pause", "unpause", "delete", "describe"):
        getattr(client.handle, method).side_effect = _missing()
    timers = _timers(client)

    await timers.pause("acme", "nightly", note="maintenance")
    await timers.delete("acme", "nightly")

    assert await timers.resume("acme", "nightly") is None
    assert await timers.next_run_at("acme", "nightly") is None


async def test_resume_returns_the_next_slot_and_paused_timers_have_none() -> None:
    assert await _timers(_Client()).resume("acme", "nightly") == NEXT_RUN
    assert await _timers(_Client(paused=True)).next_run_at("acme", "nightly") is None


@pytest.mark.parametrize("method", ["pause", "unpause", "delete", "describe", "update"])
async def test_an_unreachable_temporal_is_unavailable_with_ids_only(method: str) -> None:
    client = _Client()
    client.create_schedule.side_effect = (
        ScheduleAlreadyRunningError() if method == "update" else _down()
    )
    getattr(client.handle, method).side_effect = _down()
    timers = _timers(client)
    calls = {
        "pause": lambda: timers.pause("acme", "nightly", note="maintenance"),
        "unpause": lambda: timers.resume("acme", "nightly"),
        "delete": lambda: timers.delete("acme", "nightly"),
        "describe": lambda: timers.next_run_at("acme", "nightly"),
        "update": lambda: timers.put(_schedule(), catch_up_window_seconds=60),
    }

    with pytest.raises(ScheduleTimerUnavailableError, match=TEMPORAL_ID) as raised:
        await calls[method]()

    assert isinstance(raised.value, HarborUnavailableError)
    assert isinstance(raised.value.__cause__, RPCError)


async def test_a_timer_that_was_set_survives_a_failed_read_back() -> None:
    client = _Client()
    client.handle.describe.side_effect = _down()
    timers = _timers(client)

    assert await timers.put(_schedule(), catch_up_window_seconds=60) is None
    assert await timers.resume("acme", "nightly") is None
    client.create_schedule.assert_awaited_once()
    client.handle.unpause.assert_awaited_once()


async def test_a_failed_create_is_unavailable() -> None:
    client = _Client()
    client.create_schedule.side_effect = _down()

    with pytest.raises(ScheduleTimerUnavailableError):
        await _timers(client).put(_schedule(), catch_up_window_seconds=60)


async def test_follow_starts_one_slot_workflow_per_run() -> None:
    client = _Client()
    timers = _timers(client)

    await timers.follow("acme", "nightly", "ing_1")
    client.start_workflow.side_effect = WorkflowAlreadyStartedError("id", "harborrag.schedule_slot")
    await timers.follow("acme", "nightly", "ing_1")

    call = client.start_workflow.await_args_list[0]
    assert call.args == (
        "harborrag.schedule_slot",
        ScheduleSlotInput(tenant_id="acme", schedule_id="nightly", ingestion_id="ing_1"),
    )
    assert call.kwargs["id"] == "harborrag-schedule-follow:acme:nightly:ing_1"
    assert call.kwargs["task_queue"] == QUEUE
    assert call.kwargs["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE


async def test_follow_on_an_unreachable_temporal_is_unavailable() -> None:
    client = _Client()
    client.start_workflow.side_effect = _down()

    with pytest.raises(ScheduleTimerUnavailableError):
        await _timers(client).follow("acme", "nightly", "ing_1")


async def test_connect_translates_a_failed_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        connection_module.Client, "connect", AsyncMock(side_effect=RuntimeError("refused"))
    )

    with pytest.raises(ScheduleTimerUnavailableError):
        await connect_schedule_timers(RuntimeSettings())


async def test_connect_uses_the_schedule_task_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    monkeypatch.setattr(connection_module.Client, "connect", AsyncMock(return_value=client))

    timers = await connect_schedule_timers(RuntimeSettings(schedule_task_queue="api-slots"))
    await timers.follow("acme", "nightly", "ing_1")

    assert client.start_workflow.await_args.kwargs["task_queue"] == "api-slots"
