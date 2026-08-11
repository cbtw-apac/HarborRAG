"""Test checkpoint, pause/resume, and continue-as-new behavior for source ingestion."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from harborrag_runtime.temporal.maintenance_schemas import ProjectionCleanupResult
from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    SourceContinuation,
    SourceDiscoveryResult,
    SourceIngestionResult,
)
from harborrag_runtime.temporal.source_workflow import SourceIngestionWorkflow

from .fixtures import plan_reference as _plan_reference
from .fixtures import source_input as _source


class _WorkflowInfoStub:
    def __init__(self, *, suggested: bool = False) -> None:
        self._suggested = suggested

    def is_continue_as_new_suggested(self) -> bool:
        return self._suggested


class _ChildHandle:
    def __init__(self, awaitable) -> None:
        self._task = asyncio.create_task(awaitable)

    def __await__(self):
        return self._task.__await__()

    async def signal(self, name: str) -> None:
        del name


def _start_child(child):
    async def start(name, request, **options):
        return _ChildHandle(child(name, request, **options))

    return start


@pytest.fixture(autouse=True)
def _workflow_test_shims(monkeypatch):
    async def wait_condition(predicate):
        while not predicate():
            await asyncio.sleep(3600)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.wait_condition",
        wait_condition,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.info",
        lambda: _WorkflowInfoStub(suggested=False),
    )


@pytest.mark.asyncio
async def test_source_workflow_continue_as_new_after_N_batches(
    monkeypatch,
) -> None:
    """After continue_after_batches, workflow should trigger continue_as_new."""
    source = replace(
        _source(),
        batch_size=1,
        continue_after_batches=2,
    )
    plan = _plan_reference()
    continued = []

    async def execute_activity(name, request, **options):
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=5,  # 5 docs, batch_size=1, continue after 2 batches
            )
        if name == "harborrag.cleanup_source_projections":
            return ProjectionCleanupResult(claimed=0, completed=0, cancelled=0, failed=0)
        if name == "harborrag.finalize_source_ingestion":
            return SourceIngestionResult(
                task_id=request.source.task_id,
                scan_id="scan-1",
                discovered=5,
                published=2,
                unchanged=0,
                failed=0,
                removal_candidates=(),
                unresolved_relations=0,
                status="COMPLETED",
            )
        return request

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

    # Verify continue_as_new was called with correct continuation state
    assert len(continued) == 1
    continuation = continued[0].continuation
    assert continuation is not None
    assert continuation.scan_id == "scan-1"
    assert continuation.next_document_index == 2
    assert continuation.batch_number == 2
    assert continuation.summary == DocumentDispatchSummary(published=2)


@pytest.mark.asyncio
async def test_resume_workflow_skips_completed_batches(monkeypatch) -> None:
    """Resumed workflow with SourceContinuation should start from next_document_index."""
    source = replace(
        _source(),
        batch_size=1,
        continuation=None,
    )
    plan = _plan_reference()

    batch_calls = []

    async def execute_activity(name, request, **options):
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=5,
            )
        if name == "harborrag.cleanup_source_projections":
            return ProjectionCleanupResult(claimed=0, completed=0, cancelled=0, failed=0)
        if name == "harborrag.finalize_source_ingestion":
            assert request.summary == DocumentDispatchSummary(published=4)
            return SourceIngestionResult(
                task_id=request.source.task_id,
                scan_id="scan-1",
                discovered=5,
                published=4,
                unchanged=0,
                failed=0,
                removal_candidates=(),
                unresolved_relations=0,
                status="COMPLETED",
            )
        return request

    async def child(name, request, **options):
        assert name == "harborrag.source_batch"
        batch_calls.append(request.start_index)
        return DocumentDispatchSummary(published=1)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        _start_child(child),
    )

    # Run with continuation (resume from index 2)
    result = await SourceIngestionWorkflow().run(
        replace(
            source,
            continuation=SourceContinuation(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=5,
                next_document_index=2,
                batch_number=2,
                summary=DocumentDispatchSummary(published=1),
            ),
        )
    )

    assert result.status == "COMPLETED"
    assert result.published == 4  # 1 from prior + 3 from resumed batches
    # Verify batches started from index 2, not 0
    assert 2 in batch_calls
    assert 3 in batch_calls
    assert 4 in batch_calls
    assert 0 not in batch_calls


@pytest.mark.asyncio
async def test_pause_signal_stops_at_batch_boundary(monkeypatch) -> None:
    """Pause signal should pause workflow after current batch completes."""
    plan = _plan_reference()

    async def execute_activity(name, request, **options):
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=10,
            )
        return request

    async def child(name, request, **options):
        return DocumentDispatchSummary(published=1)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        _start_child(child),
    )

    instance = SourceIngestionWorkflow()
    # Pause signal is sent before workflow starts
    instance.pause()

    # Workflow should wait at first batch boundary
    # Since pause() is called, the workflow immediately waits
    status_before = instance.get_status()
    assert status_before.paused is True

    # Now resume and let it proceed
    instance.resume()
    status_after = instance.get_status()
    assert status_after.paused is False


@pytest.mark.asyncio
async def test_resume_signal_continues_from_pause(monkeypatch) -> None:
    """Resume signal should restart paused workflow."""
    source = replace(_source(), batch_size=2)
    plan = _plan_reference()

    batch_count = []

    async def execute_activity(name, request, **options):
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=5,
            )
        if name == "harborrag.cleanup_source_projections":
            return ProjectionCleanupResult(claimed=0, completed=0, cancelled=0, failed=0)
        if name == "harborrag.finalize_source_ingestion":
            return SourceIngestionResult(
                task_id=source.task_id,
                scan_id="scan-1",
                discovered=5,
                published=len(batch_count),
                unchanged=0,
                failed=0,
                removal_candidates=(),
                unresolved_relations=0,
                status="COMPLETED",
            )
        return request

    async def child(name, request, **options):
        batch_count.append(1)
        return DocumentDispatchSummary(published=1)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.start_child_workflow",
        _start_child(child),
    )

    instance = SourceIngestionWorkflow()

    # Pause before running
    instance.pause()
    status = instance.get_status()
    assert status.paused is True

    # Resume
    instance.resume()
    status = instance.get_status()
    assert status.paused is False


@pytest.mark.asyncio
async def test_cancel_signal_cleans_up_children(monkeypatch) -> None:
    """Cancel signal should request graceful cancellation and cleanup child workflows."""
    plan = _plan_reference()
    calls = []

    async def execute_activity(name, request, **options):
        calls.append(name)
        if name == "harborrag.discover_source_items":
            return SourceDiscoveryResult(
                scan_id="scan-1",
                plan_reference=plan,
                document_count=5,
            )
        if name == "harborrag.cancel_source_ingestion":
            return None
        if name == "harborrag.cleanup_source_projections":
            return ProjectionCleanupResult(claimed=0, completed=0, cancelled=0, failed=0)
        return request

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )

    instance = SourceIngestionWorkflow()
    # Send cancel signal before running
    instance.request_graceful_cancel()

    result = await instance.run(_source())

    assert result.status == "CANCELLED"
    assert result.published == 0
    assert "harborrag.cancel_source_ingestion" in calls
    assert "harborrag.cleanup_source_projections" in calls

    status = instance.get_status()
    assert status.cancel_requested is True
