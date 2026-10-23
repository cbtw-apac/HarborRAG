"""Pause-relay teardown against the Temporal test server.

The relay forwards the parent's pause/resume to the running batch child. When the
batch closes while the relay is still persisting a pause or resume, tearing the
relay down must not turn a successful (or gracefully cancelled) run into FAILED.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from temporalio import activity, workflow
from temporalio.client import (
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowHistory,
)
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from harborrag_runtime.temporal.maintenance_schemas import ProjectionCleanupResult
from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    SourceDiscoveryResult,
    SourceFinalizationInput,
    SourceIngestionInput,
    SourceIngestionResult,
    WorkflowArtifactReference,
    WorkflowExecutionControlInput,
)
from harborrag_runtime.temporal.source_activities import SourceActivitiesMixin
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow

from .test_client import _source

pytestmark = pytest.mark.graybox

_HISTORIES = Path(__file__).parent / "histories"
_TEARDOWN_PATCH = "harborrag-source-pause-relay-teardown"


@workflow.defn(name="harborrag.source_batch")
class _Batch:
    """A batch child that finishes when told to, or on a graceful cancel."""

    def __init__(self) -> None:
        self._done = False
        self._signals: list[str] = []

    @workflow.run
    async def run(self, request: Any) -> DocumentDispatchSummary:
        del request
        await workflow.wait_condition(lambda: self._done)
        return DocumentDispatchSummary(published=1)

    @workflow.signal
    def finish(self) -> None:
        self._done = True

    @workflow.signal
    def pause(self) -> None:
        self._signals.append("pause")

    @workflow.signal
    def resume(self) -> None:
        self._signals.append("resume")

    @workflow.signal
    def request_graceful_cancel(self) -> None:
        self._done = True

    @workflow.query
    def signals(self) -> list[str]:
        return self._signals


class _Activities:
    """Stub activities; any named in ``blocking`` waits until released."""

    def __init__(self, *blocking: str) -> None:
        self.calls: list[str] = []
        self.entered = {name: asyncio.Event() for name in blocking}
        self.released = {name: asyncio.Event() for name in blocking}

    async def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.entered:
            self.entered[name].set()
            await self.released[name].wait()

    @activity.defn(name="harborrag.discover_source_items")
    async def discover(self, request: Any) -> SourceDiscoveryResult:
        del request
        await self._record("discover")
        return SourceDiscoveryResult(
            scan_id="scan-1",
            plan_reference=WorkflowArtifactReference(
                bucket="artifacts",
                key="source-plans/scan-1.json",
                sha256="0" * 64,
                byte_size=1,
                media_type="application/json",
            ),
            document_count=1,
        )

    @activity.defn(name="harborrag.pause_source_ingestion")
    async def pause(self, request: Any) -> None:
        del request
        await self._record("pause")

    @activity.defn(name="harborrag.resume_source_ingestion")
    async def resume(self, request: Any) -> None:
        del request
        await self._record("resume")

    @activity.defn(name="harborrag.pause_workflow_execution")
    async def pause_execution(self, request: Any) -> None:
        del request
        await self._record("pause_execution")

    @activity.defn(name="harborrag.unpause_workflow_execution")
    async def unpause_execution(self, request: Any) -> None:
        del request
        await self._record("unpause_execution")

    @activity.defn(name="harborrag.record_source_failure")
    async def record_failure(self, request: Any) -> None:
        del request
        await self._record("record_source_failure")

    @activity.defn(name="harborrag.cancel_source_ingestion")
    async def cancel(self, request: Any) -> None:
        del request
        await self._record("cancel")

    @activity.defn(name="harborrag.finalize_source_ingestion")
    async def finalize(self, request: SourceFinalizationInput) -> SourceIngestionResult:
        await self._record("finalize")
        return SourceIngestionResult(
            task_id=request.source.task_id,
            scan_id="scan-1",
            discovered=1,
            published=request.summary.published,
            unchanged=0,
            failed=0,
            removal_candidates=(),
            unresolved_relations=0,
        )

    @activity.defn(name="harborrag.cleanup_source_projections")
    async def cleanup(self, request: Any) -> ProjectionCleanupResult:
        del request
        await self._record("cleanup")
        return ProjectionCleanupResult(claimed=0, completed=0, cancelled=0, failed=0)

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.discover,
            self.pause,
            self.resume,
            self.pause_execution,
            self.unpause_execution,
            self.record_failure,
            self.cancel,
            self.finalize,
            self.cleanup,
        ]


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


@asynccontextmanager
async def _source_run(
    environment: WorkflowEnvironment, activities: _Activities
) -> AsyncIterator[tuple[WorkflowHandle[Any, Any], WorkflowHandle[Any, Any]]]:
    """Start one single-batch source run; yield its handle and the batch child's."""

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    source: SourceIngestionInput = replace(_source(), task_id=task_id)
    workers = [
        Worker(
            environment.client,
            task_queue=queue,
            workflows=[SourceIngestionWorkflow, _Batch],
            activities=activities.all(),
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        for queue in set(source.workflow_options.task_queues.as_tuple())
    ]
    runs = [asyncio.create_task(worker.run()) for worker in workers]
    try:
        # Activities here block on test events, not timers; skipping time would
        # only fire their timeouts.
        with environment.auto_time_skipping_disabled():
            handle = await environment.client.start_workflow(
                SourceIngestionWorkflow.run,
                source,
                id=f"harborrag-source:{task_id}",
                task_queue=source.workflow_options.task_queues.discovery,
            )
            child = environment.client.get_workflow_handle(f"harborrag-source-batch:{task_id}:0")
            await _until(lambda: _exists(child))
            yield handle, child
    finally:
        for worker in workers:
            await worker.shutdown()
        await asyncio.gather(*runs, return_exceptions=True)


async def _exists(handle: WorkflowHandle[Any, Any]) -> bool:
    try:
        await handle.describe()
    except Exception:
        return False
    return True


async def _closed(handle: WorkflowHandle[Any, Any]) -> bool:
    return (await handle.describe()).status is WorkflowExecutionStatus.COMPLETED


async def _until(check: Callable[[], Any], *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was never satisfied")


async def _status(handle: WorkflowHandle[Any, Any]) -> dict[str, Any]:
    status: dict[str, Any] = await handle.query("get_status", result_type=dict)
    return status


async def _pause_applied(handle: WorkflowHandle[Any, Any]) -> bool:
    return bool((await _status(handle))["pause_applied"])


async def _outcome(handle: WorkflowHandle[Any, Any]) -> str:
    try:
        result = await asyncio.wait_for(handle.result(), 30)
    except WorkflowFailureError as error:
        return f"FAILED {type(error.cause).__name__}: {error.cause}"
    return str(result.status)


async def run_batch_closes_while_pause_persists(
    environment: WorkflowEnvironment,
) -> tuple[_Activities, str, WorkflowHandle[Any, Any]]:
    activities = _Activities("pause")
    async with _source_run(environment, activities) as (handle, child):
        await handle.signal("pause")
        await asyncio.wait_for(activities.entered["pause"].wait(), 10)
        # The batch's last documents land while the relay persists the pause.
        await child.signal("finish")
        await _until(lambda: _closed(child))
        activities.released["pause"].set()
        # The operator's pause still holds: the run stops at the batch boundary.
        await _until(lambda: _pause_applied(handle))
        assert (await _status(handle))["status"] == "PAUSED"
        await handle.signal("resume")
        return activities, await _outcome(handle), handle


async def run_cancel_while_resume_persists(
    environment: WorkflowEnvironment,
) -> tuple[_Activities, str, WorkflowHandle[Any, Any]]:
    activities = _Activities("resume")
    async with _source_run(environment, activities) as (handle, child):
        await handle.signal("pause")
        await _until(lambda: _pause_applied(handle))
        # Cancel clears the pause, so the relay starts persisting a resume while
        # the batch child takes the forwarded cancel and closes.
        await handle.signal("request_graceful_cancel")
        await asyncio.wait_for(activities.entered["resume"].wait(), 10)
        await _until(lambda: _closed(child))
        activities.released["resume"].set()
        return activities, await _outcome(handle), handle


async def run_pause_then_resume_mid_batch(
    environment: WorkflowEnvironment,
) -> tuple[_Activities, str, WorkflowHandle[Any, Any]]:
    activities = _Activities()
    async with _source_run(environment, activities) as (handle, child):
        await handle.signal("pause")
        await _until(lambda: _pause_applied(handle))
        await handle.signal("resume")
        # Let the relay finish forwarding the resume before the batch closes.
        await _until(lambda: _received(child, "resume"))
        await child.signal("finish")
        return activities, await _outcome(handle), handle


async def _received(child: WorkflowHandle[Any, Any], signal: str) -> bool:
    return signal in await child.query("signals", result_type=list)


@pytest.mark.asyncio
async def test_batch_closing_while_the_relay_persists_a_pause_pauses_at_the_boundary(
    environment: WorkflowEnvironment,
) -> None:
    activities, outcome, handle = await run_batch_closes_while_pause_persists(environment)

    assert outcome == "COMPLETED"
    assert "record_source_failure" not in activities.calls
    assert activities.calls[-2:] == ["finalize", "cleanup"]
    history = await handle.fetch_history()
    assert _TEARDOWN_PATCH in _patch_ids(history)
    assert await _replay_failure(history) is None


@pytest.mark.asyncio
async def test_cancel_while_the_relay_persists_a_resume_cancels_the_run(
    environment: WorkflowEnvironment,
) -> None:
    activities, outcome, handle = await run_cancel_while_resume_persists(environment)

    assert outcome == "CANCELLED"
    assert "record_source_failure" not in activities.calls
    assert "cancel" in activities.calls
    history = await handle.fetch_history()
    assert _TEARDOWN_PATCH in _patch_ids(history)
    assert await _replay_failure(history) is None


@pytest.mark.asyncio
async def test_pause_and_resume_mid_batch_complete_the_run(
    environment: WorkflowEnvironment,
) -> None:
    activities, outcome, handle = await run_pause_then_resume_mid_batch(environment)

    assert outcome == "COMPLETED"
    assert activities.calls.count("pause") == 1
    assert activities.calls.count("resume") == 1
    # An idle relay is torn down as before, so ordinary histories carry no marker.
    assert _TEARDOWN_PATCH not in _patch_ids(await handle.fetch_history())


def test_the_pre_patch_histories_are_present() -> None:
    # An empty parametrize set skips silently; the replay check must not vanish.
    assert len(list(_HISTORIES.glob("*.json"))) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history",
    sorted(path.name for path in _HISTORIES.glob("*.json")),
)
async def test_histories_recorded_before_the_relay_teardown_patch_still_replay(
    history: str,
) -> None:
    # Captured from the pre-patch workflow; the first two closed FAILED there.
    recorded = WorkflowHistory.from_json(
        "harborrag-source:replay", (_HISTORIES / history).read_text()
    )

    assert await _replay_failure(recorded) is None


def _patch_ids(history: WorkflowHistory) -> set[str]:
    return {
        json.loads(payload.data)["id"]
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
        for payload in event.marker_recorded_event_attributes.details["patch-data"].payloads
    }


async def _replay_failure(history: WorkflowHistory) -> Exception | None:
    replayer = Replayer(
        workflows=[SourceIngestionWorkflow, _Batch],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    return result.replay_failure


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RPCStatusCode.FAILED_PRECONDITION, RPCStatusCode.NOT_FOUND])
@pytest.mark.parametrize("operation", ["pause_workflow_execution", "unpause_workflow_execution"])
async def test_native_control_of_a_closed_batch_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, status: RPCStatusCode, operation: str
) -> None:
    # The relay now finishes its step after the batch closes; the native call
    # then targets a closed child and must not retry until it fails the run.
    activities = SourceActivitiesMixin()
    activities._temporal_client = SimpleNamespace(
        namespace="harborrag",
        workflow_service=SimpleNamespace(
            **{operation: AsyncMock(side_effect=RPCError("workflow closed", status, b""))}
        ),
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_activities.activity.info",
        lambda: SimpleNamespace(workflow_id="harborrag-source:task-1", activity_id="7"),
    )

    await getattr(activities, operation)(
        WorkflowExecutionControlInput(workflow_id="harborrag-source-batch:task-1:0")
    )
