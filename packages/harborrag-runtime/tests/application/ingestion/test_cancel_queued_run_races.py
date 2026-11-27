"""A queued run cancelled before its workflow exists is never started afterwards."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from runtime_ingestion.fixtures.release import (
    build_control_plane,
    build_release_resources,
    build_release_service,
)
from temporalio.exceptions import ApplicationError, ApplicationErrorCategory
from temporalio.testing import ActivityEnvironment

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.contracts.errors import HarborConnectionError
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.ingestion.run_contracts import PreparedSourceSubmission
from harborrag_runtime.application.ingestion.service import IngestionApplicationService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import WorkflowNotFoundError, WorkflowSubmissionError
from harborrag_runtime.execution.task_registry import IngestionTaskRegistry
from harborrag_runtime.ingestion import SourceIngestionService
from harborrag_runtime.ingestion.connections import ConnectionHealth
from harborrag_runtime.ingestion_contracts import IngestionExecutionReference
from harborrag_runtime.temporal.gateway import to_temporal_source
from harborrag_runtime.temporal.ingestion_activities import IngestionActivities

from ..application_fixtures import (
    FakeTemporalClient,
    public_ingestion_command,
    public_ingestion_source_input,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _RacingRegistry(IngestionTaskRegistry):
    """Task registry that lets a racing caller run at one step of a cancel or a recovery.

    Args:
        control: Control plane the registry reads and writes.
    """

    def __init__(self, control: IngestionControlPlaneDatabase) -> None:
        super().__init__(control)
        self.after_listing: Callable[[], Awaitable[object]] | None = None
        self.before_closing: Callable[[], Awaitable[object]] | None = None
        self.raced: list[object] = []

    async def pending_submissions(self, *, limit: int = 100) -> tuple[IngestionTask, ...]:
        """List the pending runs, then let the racing caller run."""
        pending = await super().pending_submissions(limit=limit)
        if self.after_listing is not None:
            self.raced.append(await self.after_listing())
        return pending

    async def close_abandoned(
        self, task_id: str, state: IngestionTaskState, *, reason: str
    ) -> None:
        """Let the racing caller run, then close the run."""
        if self.before_closing is not None:
            self.raced.append(await self.before_closing())
        await super().close_abandoned(task_id, state, reason=reason)


def _service(
    registry: IngestionTaskRegistry, temporal: FakeTemporalClient
) -> IngestionApplicationService:
    async def runtime_client() -> FakeTemporalClient:
        return temporal

    async def task_store() -> IngestionTaskRegistry:
        return registry

    return IngestionApplicationService(
        RuntimeSettings(tenant_id="default"),
        client_provider=runtime_client,
        task_store_provider=task_store,
        source_input_builder=public_ingestion_source_input,
    )


async def _queue_unstarted(
    service: IngestionApplicationService,
    control: IngestionControlPlaneDatabase,
    temporal: FakeTemporalClient,
) -> str:
    """Register a run whose workflow start failed and that Temporal does not know."""
    temporal.start_ingestion = AsyncMock(  # type: ignore[method-assign]
        side_effect=WorkflowSubmissionError("temporal unreachable")
    )
    with pytest.raises(HarborConnectionError):
        await service.submit(public_ingestion_command(), idempotency_key=None)
    del temporal.start_ingestion
    (task,) = await control.tasks.pending_submissions()
    temporal.statuses[task.task_id] = WorkflowNotFoundError("task not found")
    return task.task_id


async def test_recovery_that_listed_a_run_before_its_cancel_does_not_start_it(
    tmp_path: Path,
) -> None:
    async with build_control_plane(tmp_path) as control:
        temporal = FakeTemporalClient()
        registry = _RacingRegistry(control)
        service = _service(registry, temporal)
        task_id = await _queue_unstarted(service, control, temporal)
        registry.after_listing = lambda: service.cancel(task_id, actor="u_alex")

        recovered = await service.recover_pending_submissions()

        task = await control.tasks.get(task_id)
    assert recovered == 0
    assert temporal.started == []
    assert task is not None and task.status is IngestionTaskState.CANCELLED


async def test_recovery_while_a_cancel_holds_the_claim_does_not_start_the_run(
    tmp_path: Path,
) -> None:
    async with build_control_plane(tmp_path) as control:
        temporal = FakeTemporalClient()
        registry = _RacingRegistry(control)
        service = _service(registry, temporal)
        task_id = await _queue_unstarted(service, control, temporal)
        registry.before_closing = service.recover_pending_submissions

        await service.cancel(task_id, actor="u_alex")

        task = await control.tasks.get(task_id)
        recovered_later = await service.recover_pending_submissions()
    assert registry.raced == [0]
    assert recovered_later == 0
    assert temporal.started == []
    assert task is not None and task.status is IngestionTaskState.CANCELLED


class _StartRacedByCancel(FakeTemporalClient):
    """Gateway whose source start is cancelled through the service before Temporal has it."""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_during_start: Callable[[str], Awaitable[object]] | None = None

    async def start_ingestion(
        self, source: PreparedSourceSubmission
    ) -> IngestionExecutionReference:
        """Let the cancel run while the workflow does not exist yet, then start it."""
        if self.cancel_during_start is not None:
            self.statuses[source.task_id] = WorkflowNotFoundError("task not found")
            await self.cancel_during_start(source.task_id)
            del self.statuses[source.task_id]
        return await super().start_ingestion(source)


class _NoPlans:
    async def find(self, **kwargs: object) -> None:
        del kwargs


class _UnreadConnector:
    """A connector the refused run must never walk."""

    def discover(self, query: object) -> object:
        raise AssertionError("a cancelled run walked its source")


def _activities(service: SourceIngestionService) -> IngestionActivities:
    async def connector_for_run(**kwargs: object) -> _UnreadConnector:
        del kwargs
        return _UnreadConnector()

    runtime = SimpleNamespace(
        sources=service,
        source_plans=_NoPlans(),
        connector=connector_for_run,
        connection_health=ConnectionHealth(None),
    )
    return IngestionActivities(cast(Any, runtime))


def _environment() -> ActivityEnvironment:
    environment = ActivityEnvironment()
    environment.info = replace(
        environment.info,
        start_to_close_timeout=timedelta(hours=1),
        heartbeat_timeout=timedelta(minutes=2),
    )
    return environment


async def test_a_workflow_a_submit_starts_after_the_cancel_stops_at_its_first_activity(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        temporal = _StartRacedByCancel()
        service = _service(IngestionTaskRegistry(control), temporal)
        temporal.cancel_during_start = lambda task_id: service.cancel(task_id, actor="u_alex")
        sources = SourceIngestionService(
            control=control, documents=build_release_service(resources)
        )

        await service.submit(public_ingestion_command(), idempotency_key=None)
        (started,) = temporal.started
        with pytest.raises(ApplicationError) as refused:
            await _environment().run(
                _activities(sources).discover_source_items, to_temporal_source(started)
            )

        task = await control.tasks.get(started.task_id)
        scan_running = await control.source_scans.has_running_scan(started.source_scope_id)
    assert refused.value.type == "source_discovery_cancelled"
    assert refused.value.non_retryable
    assert refused.value.category is ApplicationErrorCategory.BENIGN
    assert task is not None and task.status is IngestionTaskState.CANCELLED
    assert task.summary["error_code"] == "workflow_not_found"
    assert scan_running is False
