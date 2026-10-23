"""Scheduled ingestion wrapper outcomes against the Temporal test server."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import fields, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission, SourceSubmission
from harborrag_runtime.temporal import ingestion_activities as activity_module
from harborrag_runtime.temporal.schemas import (
    ScheduledRunMetricInput,
    ScheduledSourceIngestionInput,
    ScheduledSourceIngestionResult,
    ScheduledSourcePreparation,
    ScheduledSourcePreparationInput,
    SourceIngestionInput,
    SourceIngestionResult,
)
from harborrag_runtime.temporal.source_workflow import ScheduledSourceIngestionWorkflow

from .test_client import _source, _source_result

pytestmark = pytest.mark.graybox

_DISCOVERY = "harborrag-discovery"


@workflow.defn(name="harborrag.source_ingestion")
class _ChildIngestion:
    """Stand-in child whose behaviour is chosen by the connection ID."""

    @workflow.run
    async def run(self, source: SourceIngestionInput) -> SourceIngestionResult:
        if source.connection_id == "hangs":
            await workflow.wait_condition(lambda: False)
        if source.connection_id == "fails":
            raise ApplicationError("discovery failed", non_retryable=True)
        status = "PARTIAL" if source.connection_id == "partial" else "COMPLETED"
        return replace(_source_result(), task_id=source.task_id, status=status)


class _Activities:
    def __init__(self) -> None:
        self.outcomes: list[str] = []
        self.prepared: list[ScheduledSourcePreparationInput] = []

    @activity.defn(name="harborrag.prepare_scheduled_source")
    async def prepare(self, request: ScheduledSourcePreparationInput) -> ScheduledSourcePreparation:
        self.prepared.append(request)
        if request.connection_id == "busy":
            return ScheduledSourcePreparation(source=None, source_scope_id="scope-busy")
        source = replace(_source(), task_id=request.task_id, connection_id=request.connection_id)
        return ScheduledSourcePreparation(source=source, source_scope_id=source.source_scope_id)

    @activity.defn(name="harborrag.record_scheduled_run_metrics")
    async def record(self, request: ScheduledRunMetricInput) -> None:
        self.outcomes.append(request.outcome)


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
    environment: WorkflowEnvironment, connection_id: str, *, cancel: bool = False
) -> tuple[_Activities, ScheduledSourceIngestionResult | BaseException]:
    activities = _Activities()
    async with Worker(
        environment.client,
        task_queue=_DISCOVERY,
        workflows=[ScheduledSourceIngestionWorkflow, _ChildIngestion],
        activities=[activities.prepare, activities.record],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await environment.client.start_workflow(
            ScheduledSourceIngestionWorkflow.run,
            ScheduledSourceIngestionInput(
                schedule_id="daily-sync", tenant_id="tenant-1", connection_id=connection_id
            ),
            id=f"harborrag-scheduled-source:daily-sync-{uuid.uuid4()}",
            task_queue=_DISCOVERY,
        )
        if cancel:
            while "started" not in activities.outcomes or not activities.prepared:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            await handle.cancel()
            # Time skipping would jump past the 10s metric-activity timeout while a
            # slow runner is still executing it, losing the "cancelled" outcome.
            with environment.auto_time_skipping_disabled():
                return await _outcome(activities, handle)
        return await _outcome(activities, handle)


async def _outcome(
    activities: _Activities, handle: Any
) -> tuple[_Activities, ScheduledSourceIngestionResult | BaseException]:
    try:
        return activities, await handle.result()
    except WorkflowFailureError as error:
        return activities, error


@pytest.mark.asyncio
async def test_completed_run_prepares_the_source_on_the_worker(
    environment: WorkflowEnvironment,
) -> None:
    activities, result = await _run(environment, "local-docs")

    assert isinstance(result, ScheduledSourceIngestionResult)
    assert result.outcome == "completed"
    assert result.result is not None and result.result.task_id == result.task_id
    assert activities.prepared[0].task_id == result.task_id
    assert activities.outcomes == ["started", "completed"]


@pytest.mark.asyncio
async def test_busy_source_scope_skips_the_run_without_failing(
    environment: WorkflowEnvironment,
) -> None:
    activities, result = await _run(environment, "busy")

    assert isinstance(result, ScheduledSourceIngestionResult)
    assert result.outcome == "skipped"
    assert activities.outcomes == ["started", "skipped"]


@pytest.mark.asyncio
async def test_partial_child_result_is_recorded_as_partial(
    environment: WorkflowEnvironment,
) -> None:
    activities, result = await _run(environment, "partial")

    assert isinstance(result, ScheduledSourceIngestionResult)
    assert result.outcome == "partial"
    assert activities.outcomes == ["started", "partial"]


@pytest.mark.asyncio
async def test_failed_child_fails_the_run(environment: WorkflowEnvironment) -> None:
    activities, result = await _run(environment, "fails")

    assert isinstance(result, WorkflowFailureError)
    assert activities.outcomes == ["started", "failed"]


@pytest.mark.asyncio
async def test_cancelled_run_is_recorded_as_cancelled(environment: WorkflowEnvironment) -> None:
    activities, result = await _run(environment, "hangs", cancel=True)

    assert isinstance(result, WorkflowFailureError)
    assert activities.outcomes == ["started", "cancelled"]


def _prepared(submission: SourceSubmission) -> PreparedSourceSubmission:
    source = _source()
    values = {field.name: getattr(source, field.name) for field in fields(PreparedSourceSubmission)}
    return PreparedSourceSubmission(**{**values, "task_id": submission.task_id})


def _activities(
    monkeypatch: pytest.MonkeyPatch,
    *,
    busy: bool,
    prepare: Callable[[SourceSubmission], PreparedSourceSubmission] = _prepared,
) -> tuple[activity_module.IngestionActivities, SimpleNamespace]:
    monkeypatch.setattr(
        activity_module,
        "prepare_configured_source_submission",
        lambda _settings, submission: prepare(submission),
    )
    scans = SimpleNamespace(has_running_scan=AsyncMock(return_value=busy))
    runtime: Any = SimpleNamespace(control=SimpleNamespace(source_scans=scans), source_plans=None)
    activities = activity_module.IngestionActivities(
        runtime,
        settings=RuntimeSettings(),
        temporal_config=TemporalRuntimeConfig(
            task_queues=replace(TemporalRuntimeConfig().task_queues, discovery="worker-discovery")
        ),
    )
    return activities, scans


def _preparation_input() -> ScheduledSourcePreparationInput:
    return ScheduledSourcePreparationInput(
        task_id="scheduled-1", tenant_id="tenant-1", connection_id="local-docs"
    )


@pytest.mark.asyncio
async def test_prepare_activity_builds_a_fresh_source_with_worker_options(monkeypatch) -> None:
    activities, scans = _activities(monkeypatch, busy=False)

    preparation = await activities.prepare_scheduled_source(_preparation_input())

    assert preparation.source is not None
    assert preparation.source.task_id == "scheduled-1"
    assert preparation.source.configuration_fingerprint == "config-v1"
    assert preparation.source.workflow_options.task_queues.discovery == "worker-discovery"
    scans.has_running_scan.assert_awaited_once_with("docs")


@pytest.mark.asyncio
async def test_prepare_activity_reports_a_busy_scope(monkeypatch) -> None:
    activities, _ = _activities(monkeypatch, busy=True)

    preparation = await activities.prepare_scheduled_source(_preparation_input())

    assert preparation.source is None
    assert preparation.source_scope_id == "docs"


@pytest.mark.asyncio
async def test_prepare_activity_fails_permanently_on_bad_connector_config(monkeypatch) -> None:
    def broken(submission: SourceSubmission) -> PreparedSourceSubmission:
        raise ValueError("Configured connector 'local-docs' is disabled")

    activities, _ = _activities(monkeypatch, busy=False, prepare=broken)

    with pytest.raises(ApplicationError, match="disabled") as raised:
        await activities.prepare_scheduled_source(_preparation_input())
    assert raised.value.non_retryable is True
