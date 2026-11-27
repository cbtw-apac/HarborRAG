"""Cancelling a document run in Temporal records it as cancelled, and its batch carries on."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
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

from harborrag_runtime.temporal.document_workflow import DocumentIngestionWorkflow
from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    DocumentFailureInput,
    DocumentIngestionInput,
    SourceBatchInput,
    WorkflowArtifactReference,
)
from harborrag_runtime.temporal.source_batch_workflow import SourceBatchWorkflow
from harborrag_runtime.temporal_models import TemporalWorkflowOptions

pytestmark = pytest.mark.graybox

_CANCEL_PATCH = "harborrag-document-records-cancellation"
_PROMPT_CANCEL_PATCH = "harborrag-batch-prompt-cancel"
_PLAN = WorkflowArtifactReference(
    bucket="artifacts",
    key="source-plans/scan-1.json",
    sha256="0" * 64,
    byte_size=1,
    media_type="application/json",
)


class _Activities:
    """A capture that waits until released, and a failure recorder."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.released = asyncio.Event()
        self.failures: list[str] = []

    @activity.defn(name="harborrag.fetch_and_capture_raw")
    async def capture(self, request: Any) -> None:
        del request
        self.entered.set()
        await self.released.wait()

    @activity.defn(name="harborrag.record_document_failure")
    async def record_failure(self, request: DocumentFailureInput) -> None:
        self.failures.append(request.error_type)

    def all(self) -> list[Callable[..., Any]]:
        return [self.capture, self.record_failure]


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


async def _run_workers(
    environment: WorkflowEnvironment, activities: _Activities
) -> tuple[list[Worker], list[asyncio.Task[None]]]:
    workers = [
        Worker(
            environment.client,
            task_queue=queue,
            workflows=[SourceBatchWorkflow, DocumentIngestionWorkflow],
            activities=activities.all(),
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        for queue in set(TemporalWorkflowOptions().task_queues.as_tuple())
    ]
    return workers, [asyncio.create_task(worker.run()) for worker in workers]


async def _stop(
    activities: _Activities, workers: list[Worker], runs: list[asyncio.Task[None]]
) -> None:
    activities.released.set()
    for worker in workers:
        await worker.shutdown()
    await asyncio.gather(*runs, return_exceptions=True)


async def _closed_status(handle: WorkflowHandle[Any, Any]) -> WorkflowExecutionStatus | None:
    try:
        await asyncio.wait_for(handle.result(), 30)
    except WorkflowFailureError:
        pass
    return (await handle.describe()).status


@pytest.mark.asyncio
async def test_a_cancelled_document_run_records_its_cancellation(
    environment: WorkflowEnvironment,
) -> None:
    activities = _Activities()
    workers, runs = await _run_workers(environment, activities)
    try:
        with environment.auto_time_skipping_disabled():
            handle = await environment.client.start_workflow(
                DocumentIngestionWorkflow.run,
                DocumentIngestionInput(
                    task_id="task-1",
                    tenant_id="tenant-1",
                    connector_name="local-docs",
                    plan_reference=_PLAN,
                    document_index=0,
                ),
                id=f"harborrag-document:{uuid.uuid4().hex[:8]}",
                task_queue=TemporalWorkflowOptions().task_queues.transform,
            )
            await asyncio.wait_for(activities.entered.wait(), 10)
            await handle.cancel()
            status = await _closed_status(handle)
            history = await handle.fetch_history()
    finally:
        await _stop(activities, workers, runs)

    assert status is WorkflowExecutionStatus.CANCELED
    assert activities.failures == ["cancelled"]
    assert _CANCEL_PATCH in _patch_ids(history)
    assert await _replay_failure(history) is None


@pytest.mark.asyncio
async def test_a_batch_counts_a_cancelled_document_as_failed_and_finishes(
    environment: WorkflowEnvironment,
) -> None:
    activities = _Activities()
    workers, runs = await _run_workers(environment, activities)
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    try:
        with environment.auto_time_skipping_disabled():
            batch = await environment.client.start_workflow(
                SourceBatchWorkflow.run,
                SourceBatchInput(
                    task_id=task_id,
                    tenant_id="tenant-1",
                    connector_name="local-docs",
                    plan_reference=_PLAN,
                    start_index=0,
                    end_index=1,
                    batch_number=0,
                    document_concurrency=1,
                ),
                id=f"harborrag-source-batch:{task_id}:0",
                task_queue=TemporalWorkflowOptions().task_queues.transform,
            )
            await asyncio.wait_for(activities.entered.wait(), 10)
            await environment.client.get_workflow_handle(
                f"harborrag-document:{task_id}:0:0"
            ).cancel()
            summary = await asyncio.wait_for(batch.result(), 30)
    finally:
        await _stop(activities, workers, runs)

    assert summary == DocumentDispatchSummary(failed=1)
    assert activities.failures == ["cancelled"]


@pytest.mark.asyncio
async def test_a_graceful_cancel_cancels_the_documents_in_flight(
    environment: WorkflowEnvironment,
) -> None:
    activities = _Activities()
    workers, runs = await _run_workers(environment, activities)
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    try:
        with environment.auto_time_skipping_disabled():
            batch = await environment.client.start_workflow(
                SourceBatchWorkflow.run,
                SourceBatchInput(
                    task_id=task_id,
                    tenant_id="tenant-1",
                    connector_name="local-docs",
                    plan_reference=_PLAN,
                    start_index=0,
                    end_index=2,
                    batch_number=0,
                    document_concurrency=1,
                ),
                id=f"harborrag-source-batch:{task_id}:0",
                task_queue=TemporalWorkflowOptions().task_queues.transform,
            )
            await asyncio.wait_for(activities.entered.wait(), 10)
            await batch.signal("request_graceful_cancel")
            summary = await asyncio.wait_for(batch.result(), 10)
            history = await batch.fetch_history()
    finally:
        await _stop(activities, workers, runs)

    assert summary == DocumentDispatchSummary(failed=1)
    assert activities.failures == ["cancelled"]
    assert _PROMPT_CANCEL_PATCH in _patch_ids(history)
    assert await _replay_failure(history, SourceBatchWorkflow) is None


def _patch_ids(history: WorkflowHistory) -> set[str]:
    return {
        json.loads(payload.data)["id"]
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
        for payload in event.marker_recorded_event_attributes.details["patch-data"].payloads
    }


async def _replay_failure(
    history: WorkflowHistory, workflow: type[Any] = DocumentIngestionWorkflow
) -> Exception | None:
    replayer = Replayer(
        workflows=[workflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    return result.replay_failure
