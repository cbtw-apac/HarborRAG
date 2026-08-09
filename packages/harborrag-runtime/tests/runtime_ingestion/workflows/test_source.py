"""Durable source workflow behavior."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from harborrag_runtime.temporal.maintenance_schemas import (
    ProjectionCleanupResult,
)
from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    SourceDiscoveryResult,
    SourceIngestionResult,
)
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow

from .fixtures import (
    plan_reference as _plan_reference,
)
from .fixtures import (
    source_input as _source,
)


class _ChildHandle:
    def __init__(self, awaitable) -> None:
        self._task = asyncio.create_task(awaitable)
        self.signals: list[str] = []

    def __await__(self):
        return self._task.__await__()

    async def signal(self, name: str) -> None:
        self.signals.append(name)


def _start_child(child):
    async def start(name, request, **options):
        return _ChildHandle(child(name, request, **options))

    return start


@pytest.fixture(autouse=True)
def _workflow_wait_condition(monkeypatch):
    async def wait_condition(predicate):
        while not predicate():
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.wait_condition",
        wait_condition,
    )


@pytest.mark.asyncio
async def test_source_workflow_passes_only_plan_reference_to_children(
    monkeypatch,
) -> None:
    source = _source()
    plan = _plan_reference()
    child_requests = []

    async def execute_activity(name, request, **options):
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=3,
            )
        if name == "harborrag.cleanup_source_projections":
            return ProjectionCleanupResult(
                claimed=0,
                completed=0,
                cancelled=0,
                failed=0,
            )
        assert name == "harborrag.finalize_source_ingestion"
        return SourceIngestionResult(
            task_id="task-1",
            scan_id="scan-1",
            discovered=3,
            published=request.summary.published,
            unchanged=request.summary.unchanged,
            failed=request.summary.failed,
            removal_candidates=(),
            unresolved_relations=0,
        )

    async def child(name, request, **options):
        assert name == "harborrag.source_batch"
        child_requests.append(request)
        return DocumentDispatchSummary(published=request.end_index - request.start_index)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        _start_child(child),
    )

    result = await SourceIngestionWorkflow().run(source)

    assert result.published == 3
    assert len(child_requests) == 2
    assert all(request.plan_reference == plan for request in child_requests)


@pytest.mark.asyncio
async def test_source_workflow_continues_only_after_completed_batch(
    monkeypatch,
) -> None:
    source = replace(
        _source(),
        batch_size=1,
        continue_after_batches=1,
    )
    plan = _plan_reference()
    continued = []

    async def execute_activity(name, request, **options):
        assert name == "harborrag.discover_source_items"
        return SourceDiscoveryResult(
            scan_id="scan-1",
            plan_reference=plan,
            document_count=3,
        )

    async def child(name, request, **options):
        assert name == "harborrag.source_batch"
        return DocumentDispatchSummary(published=1)

    def continue_as_new(request):
        continued.append(request)
        raise RuntimeError("continued")

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        _start_child(child),
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.continue_as_new",
        continue_as_new,
    )

    with pytest.raises(RuntimeError, match="continued"):
        await SourceIngestionWorkflow().run(source)

    continuation = continued[0].continuation
    assert continuation is not None
    assert continuation.scan_id == "scan-1"
    assert continuation.plan_reference == plan
    assert continuation.next_document_index == 1
    assert continuation.batch_number == 1
    assert continuation.summary == DocumentDispatchSummary(published=1)


@pytest.mark.asyncio
async def test_source_workflow_gracefully_cancels_before_dispatch(
    monkeypatch,
) -> None:
    plan = _plan_reference()
    calls: list[str] = []

    async def execute_activity(name, request, **options):
        del request, options
        calls.append(name)
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=3,
            )
        if name == "harborrag.cancel_source_ingestion":
            return None
        assert name == "harborrag.cleanup_source_projections"
        return ProjectionCleanupResult(
            claimed=0,
            completed=0,
            cancelled=0,
            failed=0,
        )

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    instance = SourceIngestionWorkflow()
    instance.request_graceful_cancel()

    result = await instance.run(_source())

    assert result.status == "CANCELLED"
    assert result.published == 0
    assert calls == [
        "harborrag.discover_source_items",
        "harborrag.cancel_source_ingestion",
        "harborrag.cleanup_source_projections",
    ]
    status = instance.get_status()
    assert status.status == "CANCELLED"
    assert status.cancel_requested is True


@pytest.mark.asyncio
async def test_source_workflow_records_failure_and_reraises_when_batch_child_fails(
    monkeypatch,
) -> None:
    """A hard child-batch failure used to propagate straight out of `run()`
    with no `record_source_failure` call, leaving the control-plane task row
    stuck non-terminal even though Temporal itself considered the run
    failed. It must now be recorded before the failure is re-raised."""
    from temporalio.exceptions import ChildWorkflowError

    plan = _plan_reference()
    recorded = []

    async def execute_activity(name, request, **options):
        del options
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=3,
            )
        assert name == "harborrag.record_source_failure"
        recorded.append(request)
        return None

    async def child(name, request, **options):
        del request, options
        assert name == "harborrag.source_batch"
        raise ChildWorkflowError(
            "child failed",
            namespace="default",
            workflow_id="wf-1",
            run_id="run-1",
            workflow_type="harborrag.source_batch",
            initiated_event_id=1,
            started_event_id=2,
            retry_state=None,
        )

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        _start_child(child),
    )

    with pytest.raises(ChildWorkflowError):
        await SourceIngestionWorkflow().run(_source())

    assert len(recorded) == 1
    assert recorded[0].task_id == "task-1"
    assert recorded[0].error_code == "ChildWorkflowError"


def test_source_workflow_pause_and_resume_status_are_explicit() -> None:
    instance = SourceIngestionWorkflow()

    instance.pause()
    paused = instance.get_status()
    instance.resume()
    resumed = instance.get_status()

    assert paused.paused is True
    assert resumed.paused is False
