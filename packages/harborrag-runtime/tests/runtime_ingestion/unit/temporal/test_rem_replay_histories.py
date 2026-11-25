"""Histories recorded by earlier workflow code still replay against today's workflows.

- ``batch/pre_window_patch_*``: wave dispatch, recorded at 507f0dc7^, before the
  sliding-window and circuit-breaker patches.
- ``batch/window_*``: the patched window and breaker branches, recorded at f83e1e6d.
- ``document/``: recorded at f83e1e6d, before ``harborrag-document-records-cancellation``.
- ``source/``: a continue-as-new run and a graceful cancel, recorded at f83e1e6d, before
  ``harborrag-source-guarded-discovery-stop``.
- ``scheduled/``: recorded at this commit; the scheduled workflow has no patch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

from harborrag_runtime.temporal.document_workflow import DocumentIngestionWorkflow
from harborrag_runtime.temporal.source_batch_workflow import SourceBatchWorkflow
from harborrag_runtime.temporal.source_workflow import (
    ScheduledSourceIngestionWorkflow,
    SourceIngestionWorkflow,
)

pytestmark = pytest.mark.graybox

_HISTORIES = Path(__file__).parent / "histories"
_WORKFLOWS = {
    "batch": SourceBatchWorkflow,
    "document": DocumentIngestionWorkflow,
    "source": SourceIngestionWorkflow,
    "scheduled": ScheduledSourceIngestionWorkflow,
}
_SLIDING_WINDOW = "harborrag-batch-sliding-window"
_CIRCUIT_BREAKER = "harborrag-batch-circuit-breaker"
_RECORDED_PATCHES = {
    "batch/pre_window_patch_waves_completed.json": frozenset(),
    "batch/pre_window_patch_waves_paused_then_resumed.json": frozenset(),
    "batch/pre_window_patch_waves_graceful_cancel.json": frozenset(),
    "batch/window_completed.json": frozenset({_SLIDING_WINDOW}),
    "batch/window_circuit_breaker_paused_and_resumed.json": frozenset(
        {_SLIDING_WINDOW, _CIRCUIT_BREAKER}
    ),
    "document/pre_cancel_patch_published.json": frozenset(),
    "document/pre_cancel_patch_parser_rejected.json": frozenset(),
    "document/pre_cancel_patch_cancelled_during_fetch.json": frozenset(),
    "source/pre_guarded_stop_patch_first_run_continues_as_new.json": frozenset(),
    "source/pre_guarded_stop_patch_continued_run_finishes.json": frozenset(),
    "source/pre_guarded_stop_patch_cancelled_after_discovery.json": frozenset(),
    "scheduled/completed.json": frozenset(),
    "scheduled/skipped_scope_busy.json": frozenset(),
    "scheduled/child_failed.json": frozenset(),
}


def test_every_recorded_history_is_listed() -> None:
    recorded = {
        path.relative_to(_HISTORIES).as_posix()
        for folder in _WORKFLOWS
        for path in (_HISTORIES / folder).glob("*.json")
    }

    assert recorded == set(_RECORDED_PATCHES)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_RECORDED_PATCHES))
async def test_recorded_history_replays_against_todays_workflows(name: str) -> None:
    history = _history(name)

    assert _patch_ids(history) == _RECORDED_PATCHES[name]
    replayer = Replayer(
        workflows=[_WORKFLOWS[name.partition("/")[0]]],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is None


def test_the_continue_as_new_histories_are_one_run_and_its_continuation() -> None:
    first = _history("source/pre_guarded_stop_patch_first_run_continues_as_new.json")
    continued = _history("source/pre_guarded_stop_patch_continued_run_finishes.json")

    assert first.events[-1].event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
    assert (
        continued.events[0].workflow_execution_started_event_attributes.continued_execution_run_id
        == first.run_id
    )


def _history(name: str) -> WorkflowHistory:
    """Load a history under the workflow ID it was recorded with.

    The scheduled workflow derives its task ID, and so its child's ID, from its own
    workflow ID.
    """
    path = _HISTORIES / name
    folder = name.partition("/")[0]
    workflow_id = (
        f"harborrag-scheduled-source:sched-replay-{path.stem}"
        if folder == "scheduled"
        else f"harborrag-{folder}:replay"
    )
    return WorkflowHistory.from_json(workflow_id, path.read_text())


def _patch_ids(history: WorkflowHistory) -> frozenset[str]:
    return frozenset(
        json.loads(payload.data)["id"]
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
        for payload in event.marker_recorded_event_attributes.details["patch-data"].payloads
    )
