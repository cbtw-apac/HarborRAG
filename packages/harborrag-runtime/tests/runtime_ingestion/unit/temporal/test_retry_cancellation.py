"""Cancelling a retry run in Temporal records it as cancelled, wherever the cancel lands."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import (
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowHistory,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.temporal.retry_workflow import DocumentRetryWorkflow, RetryFailuresWorkflow
from harborrag_runtime.temporal.schemas import (
    RetryFailuresInput,
    RetryPreparationResult,
    SourceCancellationInput,
    WorkflowArtifactReference,
)

pytestmark = pytest.mark.graybox

_HISTORIES = Path(__file__).parent / "histories" / "retry"
_CANCEL_PATCH = "harborrag-retry-records-cancellation"


class _Activities:
    """Stub retry activities; any named in ``blocking`` waits until released."""

    def __init__(self, *blocking: str) -> None:
        self.calls: list[str] = []
        self.cancelled: list[str] = []
        self.entered = {name: asyncio.Event() for name in blocking}
        self.released = {name: asyncio.Event() for name in blocking}

    async def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.entered:
            self.entered[name].set()
            await self.released[name].wait()

    def release_all(self) -> None:
        for event in self.released.values():
            event.set()

    @activity.defn(name="harborrag.prepare_retry_failures")
    async def prepare(self, request: Any) -> RetryPreparationResult:
        del request
        await self._record("prepare")
        return RetryPreparationResult(
            plan_reference=WorkflowArtifactReference(
                bucket="artifacts",
                key="source-plans/retry.json",
                sha256="0" * 64,
                byte_size=1,
                media_type="application/json",
            ),
            document_count=2,
        )

    @activity.defn(name="harborrag.retry_document_release")
    async def release(self, request: Any) -> DocumentIngestionOutcome:
        del request
        await self._record("release")
        return DocumentIngestionOutcome.PUBLISHED

    @activity.defn(name="harborrag.record_retry_document_failure")
    async def record_document_failure(self, request: Any) -> None:
        del request
        await self._record("record_document_failure")

    @activity.defn(name="harborrag.record_retry_failures_task_failure")
    async def record_task_failure(self, request: Any) -> None:
        del request
        await self._record("record_task_failure")

    @activity.defn(name="harborrag.finalize_retry_failures")
    async def finalize(self, request: Any) -> None:
        del request
        await self._record("finalize")

    @activity.defn(name="harborrag.cancel_source_ingestion")
    async def cancel(self, request: SourceCancellationInput) -> None:
        self.cancelled.append(request.task_id)
        await self._record("cancel")

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.prepare,
            self.release,
            self.record_document_failure,
            self.record_task_failure,
            self.finalize,
            self.cancel,
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
async def _retry_run(
    environment: WorkflowEnvironment, activities: _Activities
) -> AsyncIterator[WorkflowHandle[Any, Any]]:
    """Start one two-document retry run and yield its handle."""

    retry_task_id = f"retry-{uuid.uuid4().hex[:8]}"
    request = RetryFailuresInput(
        retry_task_id=retry_task_id,
        original_task_id="task-1",
        tenant_id="tenant-1",
        document_ids=("document-1", "document-2"),
        document_concurrency=2,
    )
    workers = [
        Worker(
            environment.client,
            task_queue=queue,
            workflows=[RetryFailuresWorkflow, DocumentRetryWorkflow],
            activities=activities.all(),
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        for queue in set(request.workflow_options.task_queues.as_tuple())
    ]
    runs = [asyncio.create_task(worker.run()) for worker in workers]
    try:
        with environment.auto_time_skipping_disabled():
            yield await environment.client.start_workflow(
                RetryFailuresWorkflow.run,
                request,
                id=f"harborrag-retry:{retry_task_id}",
                task_queue=request.workflow_options.task_queues.discovery,
            )
    finally:
        activities.release_all()
        for worker in workers:
            await worker.shutdown()
        await asyncio.gather(*runs, return_exceptions=True)


async def _closed_status(handle: WorkflowHandle[Any, Any]) -> WorkflowExecutionStatus | None:
    try:
        await asyncio.wait_for(handle.result(), 30)
    except WorkflowFailureError:
        pass
    return (await handle.describe()).status


async def run_cancel_while_documents_retry(
    environment: WorkflowEnvironment,
) -> tuple[_Activities, WorkflowExecutionStatus | None, WorkflowHandle[Any, Any]]:
    activities = _Activities("release")
    async with _retry_run(environment, activities) as handle:
        await asyncio.wait_for(activities.entered["release"].wait(), 10)
        await handle.cancel()
        return activities, await _closed_status(handle), handle


async def run_cancel_while_preparing(
    environment: WorkflowEnvironment,
) -> tuple[_Activities, WorkflowExecutionStatus | None, WorkflowHandle[Any, Any]]:
    activities = _Activities("prepare")
    async with _retry_run(environment, activities) as handle:
        await asyncio.wait_for(activities.entered["prepare"].wait(), 10)
        await handle.cancel()
        return activities, await _closed_status(handle), handle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [run_cancel_while_documents_retry, run_cancel_while_preparing],
    ids=["documents", "preparation"],
)
async def test_a_cancelled_retry_run_records_its_cancellation(
    environment: WorkflowEnvironment, scenario: Any
) -> None:
    activities, status, handle = await scenario(environment)

    assert status is WorkflowExecutionStatus.CANCELED
    assert activities.cancelled == [handle.id.removeprefix("harborrag-retry:")]
    assert "record_task_failure" not in activities.calls
    history = await handle.fetch_history()
    assert _CANCEL_PATCH in _patch_ids(history)
    assert await _replay_failure(history) is None


def test_the_pre_patch_histories_are_present() -> None:
    assert len(list(_HISTORIES.glob("pre_retry_cancel_patch_*.json"))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history",
    sorted(path.name for path in _HISTORIES.glob("pre_retry_cancel_patch_*.json")),
)
async def test_histories_recorded_before_the_cancel_patch_still_replay(history: str) -> None:
    recorded = WorkflowHistory.from_json(
        "harborrag-retry:replay", (_HISTORIES / history).read_text()
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
        workflows=[RetryFailuresWorkflow, DocumentRetryWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    return result.replay_failure
