"""Cancelling a source run in Temporal records it as cancelled, wherever the cancel lands."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity, workflow
from temporalio.client import (
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowHistory,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    Interceptor,
    Replayer,
    SignalChildWorkflowInput,
    UnsandboxedWorkflowRunner,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from harborrag_runtime.temporal.maintenance_schemas import ProjectionCleanupResult
from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    SourceDiscoveryResult,
    SourceFailureInput,
    SourceFinalizationInput,
    SourceIngestionInput,
    SourceIngestionResult,
    WorkflowArtifactReference,
)
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow

from .test_client import _source

pytestmark = pytest.mark.graybox

_CANCEL_PATCH = "harborrag-source-records-cancellation"
_STOP_CHECK_PATCH = "harborrag-source-guarded-discovery-stop"
_CLOSED_BATCH_PATCH = "harborrag-source-cancel-closed-batch"
_DISCOVERY_SKIPS = {"unsupported_extension": 1}
_RUN_SKIPS = {"EXTRACTION_FAILED": 2, "unsupported_extension": 1}


@workflow.defn(name="harborrag.source_batch")
class _Batch:
    """A batch child that finishes when told to, or on a graceful cancel."""

    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self, request: Any) -> DocumentDispatchSummary:
        del request
        await workflow.wait_condition(lambda: self._done)
        return DocumentDispatchSummary(published=1)

    @workflow.signal
    def finish(self) -> None:
        self._done = True

    @workflow.signal
    def request_graceful_cancel(self) -> None:
        self._done = True


@dataclass
class _Activities:
    """Stub activities; any named in ``blocking`` waits until released."""

    blocking: tuple[str, ...] = ()
    fail_pause: bool = False
    calls: list[str] = field(default_factory=list)
    failure_codes: list[str] = field(default_factory=list)
    entered: dict[str, asyncio.Event] = field(default_factory=dict)
    released: dict[str, asyncio.Event] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.entered = {name: asyncio.Event() for name in self.blocking}
        self.released = {name: asyncio.Event() for name in self.blocking}

    async def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.entered:
            self.entered[name].set()
            await self.released[name].wait()

    def release_all(self) -> None:
        for event in self.released.values():
            event.set()

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
            skipped_by_reason=dict(_DISCOVERY_SKIPS),
        )

    @activity.defn(name="harborrag.pause_source_ingestion")
    async def pause(self, request: Any) -> None:
        del request
        await self._record("pause")
        if self.fail_pause:
            raise ApplicationError("control plane unavailable", non_retryable=True)

    @activity.defn(name="harborrag.resume_source_ingestion")
    async def resume(self, request: Any) -> None:
        del request
        await self._record("resume")

    @activity.defn(name="harborrag.record_source_failure")
    async def record_failure(self, request: SourceFailureInput) -> None:
        self.failure_codes.append(request.error_code)
        await self._record("record_source_failure")

    @activity.defn(name="harborrag.cancel_source_ingestion")
    async def cancel(self, request: Any) -> dict[str, object]:
        del request
        await self._record("cancel")
        return {"skipped_by_reason": dict(_RUN_SKIPS)}

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
            self.record_failure,
            self.cancel,
            self.finalize,
            self.cleanup,
        ]


class _HeldCancelSignal(Interceptor):
    """Holds the parent's graceful-cancel signal to its batch until the test releases it.

    The workflow task stays open meanwhile, so the batch can close before the signal is sent.
    The hold blocks only the parent's activation thread. A worker in debug mode runs
    activations on the event loop instead, which stalls the batch and the test until the
    task times out.
    """

    def __init__(self) -> None:
        self.held = threading.Event()
        self.release = threading.Event()

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        del input
        gate = self

        class _Outbound(WorkflowOutboundInterceptor):
            async def signal_child_workflow(self, input: SignalChildWorkflowInput) -> None:
                if input.signal == "request_graceful_cancel":
                    gate.held.set()
                    gate.release.wait(10)
                await super().signal_child_workflow(input)

        class _Inbound(WorkflowInboundInterceptor):
            def init(self, outbound: WorkflowOutboundInterceptor) -> None:
                super().init(_Outbound(outbound))

        return _Inbound


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
    environment: WorkflowEnvironment,
    activities: _Activities,
    interceptors: tuple[Interceptor, ...] = (),
) -> AsyncIterator[tuple[WorkflowHandle[Any, Any], WorkflowHandle[Any, Any]]]:
    """Start one single-batch source run; yield its handle and its batch child's."""

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    source: SourceIngestionInput = replace(_source(), task_id=task_id)
    workers = [
        Worker(
            environment.client,
            task_queue=queue,
            workflows=[SourceIngestionWorkflow, _Batch],
            activities=activities.all(),
            workflow_runner=UnsandboxedWorkflowRunner(),
            interceptors=interceptors,
        )
        for queue in set(source.workflow_options.task_queues.as_tuple())
    ]
    runs = [asyncio.create_task(worker.run()) for worker in workers]
    try:
        with environment.auto_time_skipping_disabled():
            handle = await environment.client.start_workflow(
                SourceIngestionWorkflow.run,
                source,
                id=f"harborrag-source:{task_id}",
                task_queue=source.workflow_options.task_queues.discovery,
            )
            yield (
                handle,
                environment.client.get_workflow_handle(f"harborrag-source-batch:{task_id}:0"),
            )
    finally:
        activities.release_all()
        for worker in workers:
            await worker.shutdown()
        await asyncio.gather(*runs, return_exceptions=True)


async def _until(check: Callable[[], Any], *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was never satisfied")


async def _exists(handle: WorkflowHandle[Any, Any]) -> bool:
    try:
        await handle.describe()
    except Exception:
        return False
    return True


async def _closed(handle: WorkflowHandle[Any, Any]) -> bool:
    return (await handle.describe()).status is WorkflowExecutionStatus.COMPLETED


async def _paused(handle: WorkflowHandle[Any, Any]) -> bool:
    status: dict[str, Any] = await handle.query("get_status", result_type=dict)
    return bool(status["pause_applied"])


async def _close(handle: WorkflowHandle[Any, Any]) -> tuple[WorkflowExecutionStatus | None, Any]:
    try:
        result = await asyncio.wait_for(handle.result(), 30)
    except WorkflowFailureError as error:
        result = error.cause
    return (await handle.describe()).status, result


async def _cancel_during_discovery(environment: WorkflowEnvironment) -> _Outcome:
    activities = _Activities(blocking=("discover",))
    async with _source_run(environment, activities) as (handle, _):
        await asyncio.wait_for(activities.entered["discover"].wait(), 10)
        await handle.cancel()
        return _Outcome(activities, handle, *await _close(handle))


async def _cancel_during_batch(environment: WorkflowEnvironment) -> _Outcome:
    activities = _Activities()
    async with _source_run(environment, activities) as (handle, child):
        await _until(lambda: _exists(child))
        await handle.cancel()
        return _Outcome(activities, handle, *await _close(handle))


async def _cancel_during_finalization(environment: WorkflowEnvironment) -> _Outcome:
    activities = _Activities(blocking=("finalize",))
    async with _source_run(environment, activities) as (handle, child):
        await _until(lambda: _exists(child))
        await child.signal("finish")
        await asyncio.wait_for(activities.entered["finalize"].wait(), 10)
        await handle.cancel()
        return _Outcome(activities, handle, *await _close(handle))


async def _cancel_while_paused_after_discovery(environment: WorkflowEnvironment) -> _Outcome:
    activities = _Activities(blocking=("discover",))
    async with _source_run(environment, activities) as (handle, _):
        await asyncio.wait_for(activities.entered["discover"].wait(), 10)
        await handle.signal("pause")
        activities.released["discover"].set()
        await _until(lambda: _paused(handle))
        await handle.cancel()
        return _Outcome(activities, handle, *await _close(handle))


@dataclass
class _Outcome:
    activities: _Activities
    handle: WorkflowHandle[Any, Any]
    status: WorkflowExecutionStatus | None
    result: Any


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        _cancel_during_discovery,
        _cancel_during_batch,
        _cancel_during_finalization,
        _cancel_while_paused_after_discovery,
    ],
    ids=["discovery", "batch", "finalization", "paused-after-discovery"],
)
async def test_a_cancelled_source_run_records_its_cancellation(
    environment: WorkflowEnvironment, scenario: Any
) -> None:
    outcome: _Outcome = await scenario(environment)

    assert outcome.status is WorkflowExecutionStatus.CANCELED
    assert "cancel" in outcome.activities.calls
    assert "record_source_failure" not in outcome.activities.calls
    assert await _replay_failure(await outcome.handle.fetch_history()) is None


@pytest.mark.asyncio
async def test_a_cancel_landing_on_an_activity_is_recorded_behind_its_patch(
    environment: WorkflowEnvironment,
) -> None:
    outcome = await _cancel_during_finalization(environment)

    assert _CANCEL_PATCH in _patch_ids(await outcome.handle.fetch_history())


@pytest.mark.asyncio
async def test_a_pause_that_cannot_be_persisted_after_discovery_fails_the_run_recorded(
    environment: WorkflowEnvironment,
) -> None:
    activities = _Activities(blocking=("discover",), fail_pause=True)
    async with _source_run(environment, activities) as (handle, _):
        await asyncio.wait_for(activities.entered["discover"].wait(), 10)
        await handle.signal("pause")
        activities.released["discover"].set()
        status, _ = await _close(handle)
        history = await handle.fetch_history()

    assert status is WorkflowExecutionStatus.FAILED
    assert activities.calls[-1] == "record_source_failure"
    assert _STOP_CHECK_PATCH in _patch_ids(history)
    assert await _replay_failure(history) is None


@pytest.mark.asyncio
async def test_a_graceful_cancel_returns_every_skip_of_the_run(
    environment: WorkflowEnvironment,
) -> None:
    activities = _Activities()
    async with _source_run(environment, activities) as (handle, child):
        await _until(lambda: _exists(child))
        await handle.signal("request_graceful_cancel")
        status, result = await _close(handle)

    assert status is WorkflowExecutionStatus.COMPLETED
    assert result.status == "CANCELLED"
    assert result.skipped_by_reason == _RUN_SKIPS
    assert result.skipped == sum(_RUN_SKIPS.values())


@pytest.mark.asyncio
async def test_a_graceful_cancel_racing_the_batch_close_cancels_the_run(
    environment: WorkflowEnvironment,
) -> None:
    held = _HeldCancelSignal()
    activities = _Activities()
    async with _source_run(environment, activities, (held,)) as (handle, child):
        await _until(lambda: _exists(child))
        await handle.signal("request_graceful_cancel")
        assert await asyncio.to_thread(held.held.wait, 10)
        await child.signal("finish")
        await _until(lambda: _closed(child))
        held.release.set()
        status, result = await _close(handle)
        history = await handle.fetch_history()

    assert status is WorkflowExecutionStatus.COMPLETED, result
    assert result.status == "CANCELLED"
    assert "record_source_failure" not in activities.calls
    assert _CLOSED_BATCH_PATCH in _patch_ids(history)
    assert await _replay_failure(history) is None


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
