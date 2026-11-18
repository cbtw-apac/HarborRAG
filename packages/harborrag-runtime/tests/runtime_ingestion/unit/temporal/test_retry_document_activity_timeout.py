"""A retry's document activity must use the connection's carried activity timeout.

Regression for `DocumentRetryWorkflow` hardcoding a 60-minute
`start_to_close_timeout` on `harborrag.retry_document_release`: a connection
with `ingestion.activity_timeout` set (carried on `workflow_options.timeouts`)
was silently ignored on retry, even though `register_retry`/`retry_from_task`
carry the override through to the retry run.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.temporal.retry_workflow import DocumentRetryWorkflow, RetryFailuresWorkflow
from harborrag_runtime.temporal.schemas import (
    RetryFailuresInput,
    RetryPreparationResult,
    WorkflowArtifactReference,
)
from harborrag_runtime.temporal_models import ActivityTimeoutConfig, TemporalWorkflowOptions

pytestmark = pytest.mark.graybox

_CARRIED_DOCUMENT_SECONDS = 120


class _Activities:
    @activity.defn(name="harborrag.prepare_retry_failures")
    async def prepare(self, request: Any) -> RetryPreparationResult:
        del request
        return RetryPreparationResult(
            plan_reference=WorkflowArtifactReference(
                bucket="artifacts",
                key="source-plans/retry.json",
                sha256="0" * 64,
                byte_size=1,
                media_type="application/json",
            ),
            document_count=1,
        )

    @activity.defn(name="harborrag.retry_document_release")
    async def release(self, request: Any) -> DocumentIngestionOutcome:
        del request
        return DocumentIngestionOutcome.PUBLISHED

    @activity.defn(name="harborrag.record_retry_document_failure")
    async def record_document_failure(self, request: Any) -> None:
        del request

    @activity.defn(name="harborrag.record_retry_failures_task_failure")
    async def record_task_failure(self, request: Any) -> None:
        del request

    @activity.defn(name="harborrag.finalize_retry_failures")
    async def finalize(self, request: Any) -> None:
        del request

    def all(self) -> list[Any]:
        return [
            self.prepare,
            self.release,
            self.record_document_failure,
            self.record_task_failure,
            self.finalize,
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


@pytest.mark.asyncio
async def test_a_retry_schedules_its_document_activity_under_the_carried_timeout(
    environment: WorkflowEnvironment,
) -> None:
    activities = _Activities()
    retry_task_id = f"retry-{uuid.uuid4().hex[:8]}"
    request = RetryFailuresInput(
        retry_task_id=retry_task_id,
        original_task_id="task-1",
        tenant_id="tenant-1",
        document_ids=("document-1",),
        workflow_options=TemporalWorkflowOptions(
            timeouts=ActivityTimeoutConfig(document_seconds=_CARRIED_DOCUMENT_SECONDS)
        ),
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
        handle: WorkflowHandle[Any, Any] = await environment.client.start_workflow(
            RetryFailuresWorkflow.run,
            request,
            id=f"harborrag-retry:{retry_task_id}",
            task_queue=request.workflow_options.task_queues.discovery,
        )
        await handle.result()
    finally:
        for worker in workers:
            await worker.shutdown()
        await asyncio.gather(*runs, return_exceptions=True)

    # The document activity is scheduled by the child `DocumentRetryWorkflow`,
    # not the parent `RetryFailuresWorkflow` -- its history is a separate
    # workflow execution, so fetch that one.
    child_handle = environment.client.get_workflow_handle(
        f"harborrag-document-retry:{retry_task_id}:0"
    )
    history = await child_handle.fetch_history()
    scheduled_seconds = {
        event.activity_task_scheduled_event_attributes.activity_type.name: (
            event.activity_task_scheduled_event_attributes.start_to_close_timeout.ToSeconds()
        )
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    }
    assert scheduled_seconds["harborrag.retry_document_release"] == _CARRIED_DOCUMENT_SECONDS
