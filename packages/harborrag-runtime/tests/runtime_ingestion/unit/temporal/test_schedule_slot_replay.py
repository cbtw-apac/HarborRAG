"""Recorded schedule slot histories still replay against today's slot workflow.

``histories/schedule_slot/`` was recorded with the first version of
``harborrag.schedule_slot``: a slot that started and followed its run, a slot that
waited for a busy connection, and a triggered run followed without a slot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from harborrag_runtime.temporal.sandbox import workflow_sandbox_runner
from harborrag_runtime.temporal.schedule_slots import ScheduleSlotWorkflow

pytestmark = pytest.mark.graybox

_HISTORIES = Path(__file__).parent / "histories" / "schedule_slot"
_RECORDED = ("queued_then_started", "started_then_followed", "triggered_follow")


def test_every_recorded_history_is_listed() -> None:
    assert sorted(path.stem for path in _HISTORIES.glob("*.json")) == list(_RECORDED)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RECORDED)
async def test_recorded_history_replays_against_todays_slot_workflow(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"harborrag-schedule-slot:replay-{name}", (_HISTORIES / f"{name}.json").read_text()
    )
    replayer = Replayer(
        workflows=[ScheduleSlotWorkflow],
        workflow_runner=workflow_sandbox_runner([ScheduleSlotWorkflow]),
    )

    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)

    assert result.replay_failure is None
