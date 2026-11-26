"""A document retry heartbeats, so a dead attempt is retried in minutes, not after an hour."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.temporal.ingestion_activities import IngestionActivities
from harborrag_runtime.temporal.retry_workflow import DocumentRetryWorkflow
from harborrag_runtime.temporal.schemas import RetryDocumentInput, WorkflowArtifactReference

pytestmark = pytest.mark.graybox

_HEARTBEAT_PATCH = "harborrag-retry-heartbeat-timeout"
_PLAN = WorkflowArtifactReference(
    bucket="artifacts",
    key="source-plans/retry.json",
    sha256="0" * 64,
    byte_size=1,
    media_type="application/json",
)


def _request() -> RetryDocumentInput:
    return RetryDocumentInput(
        retry_task_id="retry-1",
        original_task_id="task-1",
        tenant_id="tenant-1",
        plan_reference=_PLAN,
        document_index=0,
    )


class _Release:
    """Stub retry activities that publish at once."""

    @activity.defn(name="harborrag.retry_document_release")
    async def release(self, request: Any) -> DocumentIngestionOutcome:
        del request
        return DocumentIngestionOutcome.PUBLISHED

    @activity.defn(name="harborrag.record_retry_document_failure")
    async def record_failure(self, request: Any) -> None:
        del request


@pytest.mark.asyncio
async def test_a_retry_attempt_times_out_two_minutes_after_its_last_heartbeat() -> None:
    """Without it, an attempt on a dead worker holds the retry for its whole 60-minute budget.

    The time-skipping test server does not advance its clock while an attempt is in flight, so
    the timeout is read from the scheduled activity instead of being waited out.
    """
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as error:  # pragma: no cover - depends on the test-server download
        pytest.skip(f"Temporal test server unavailable: {error}")
    activities = _Release()
    request = _request()
    try:
        async with Worker(
            environment.client,
            task_queue=request.workflow_options.task_queues.io,
            workflows=[DocumentRetryWorkflow],
            activities=[activities.release, activities.record_failure],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await environment.client.start_workflow(
                DocumentRetryWorkflow.run,
                request,
                id=f"harborrag-document-retry:{uuid.uuid4().hex[:8]}",
                task_queue=request.workflow_options.task_queues.io,
            )
            outcome = await asyncio.wait_for(handle.result(), 30)
            history = await handle.fetch_history()
    finally:
        await environment.shutdown()

    [scheduled] = [
        event.activity_task_scheduled_event_attributes
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert outcome is DocumentIngestionOutcome.PUBLISHED
    assert scheduled.heartbeat_timeout.ToTimedelta() == timedelta(minutes=2)
    assert _HEARTBEAT_PATCH in _patch_ids(history)
    assert await _replay_failure(history) is None


class _Observability:
    @contextmanager
    def boundary(self, stage: str) -> Iterator[None]:
        del stage
        yield


@pytest.mark.asyncio
async def test_the_retry_activity_heartbeats_while_it_releases_the_document() -> None:
    sources = SimpleNamespace(retry_one=_published)
    runtime = SimpleNamespace(sources=sources, source_plans=None)
    activities = IngestionActivities(cast(Any, runtime))
    activities._observability = cast(Any, _Observability())
    activities._documents = cast(Any, SimpleNamespace(get=_planned))
    environment = ActivityEnvironment()
    environment.info = replace(
        environment.info,
        start_to_close_timeout=timedelta(minutes=60),
        heartbeat_timeout=timedelta(minutes=2),
    )
    heartbeats: list[object] = []
    environment.on_heartbeat = lambda *details: heartbeats.append(details)

    outcome = await environment.run(activities.retry_document_release, _request())

    assert outcome is DocumentIngestionOutcome.PUBLISHED
    assert heartbeats


async def _planned(request: object) -> object:
    del request
    return SimpleNamespace(request=SimpleNamespace(configuration_fingerprint="config-v1"))


async def _published(**kwargs: object) -> DocumentIngestionOutcome:
    del kwargs
    return DocumentIngestionOutcome.PUBLISHED


def _patch_ids(history: WorkflowHistory) -> set[str]:
    return {
        json.loads(payload.data)["id"]
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
        for payload in event.marker_recorded_event_attributes.details["patch-data"].payloads
    }


async def _replay_failure(history: WorkflowHistory) -> Exception | None:
    replayer = Replayer(
        workflows=[DocumentRetryWorkflow], workflow_runner=UnsandboxedWorkflowRunner()
    )
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    return result.replay_failure
