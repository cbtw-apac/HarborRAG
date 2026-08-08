"""Durable source workflow behavior."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from harborrag_runtime.temporal.document_workflow import (
    DocumentIngestionWorkflow,
)
from harborrag_runtime.temporal.maintenance_schemas import (
    ProjectionCleanupResult,
)
from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
    SourceBatchInput,
    SourceDiscoveryResult,
    SourceIngestionResult,
)
from harborrag_runtime.temporal.source_workflow import (
    SourceBatchWorkflow,
    SourceIngestionWorkflow,
)

from .fixtures import (
    plan_reference as _plan_reference,
)
from .fixtures import (
    source_input as _source,
)


@pytest.mark.asyncio
async def test_document_workflow_routes_twelve_stages_to_resource_queues(
    monkeypatch,
) -> None:
    calls = []

    async def execute_activity(name, request, **options):
        calls.append((name, request, options))
        if name == "harborrag.fetch_and_capture_raw":
            return RawCaptureResult(
                document=request,
                document_id="document-1",
                document_version_id=None,
                decision="NEW",
                connector_type="local",
                content_hash="b" * 64,
                source_artifact=_plan_reference(),
                metadata_artifact=_plan_reference(),
            )
        if name == "harborrag.parse_and_normalize":
            return PreparedDocument(
                document=request.document,
                document_id="document-1",
                document_version_id="version-1",
                decision="NEW",
                canonical_reference=_plan_reference(),
            )
        if name == "harborrag.publish_version":
            return "published"
        return request

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_activity",
        execute_activity,
    )
    request = DocumentIngestionInput(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="local-docs",
        plan_reference=_plan_reference(),
        document_index=4,
    )

    result = await DocumentIngestionWorkflow().run(request)

    assert result == "published"
    assert tuple((call[0], call[2]["task_queue"]) for call in calls) == (
        ("harborrag.fetch_and_capture_raw", "harborrag-io"),
        ("harborrag.parse_and_normalize", "harborrag-parser"),
        ("harborrag.sync_content_units", "harborrag-transform"),
        ("harborrag.persist_canonical", "harborrag-io"),
        ("harborrag.chunk_and_validate", "harborrag-transform"),
        ("harborrag.encode_chunks", "harborrag-model"),
        ("harborrag.build_relations", "harborrag-transform"),
        ("harborrag.build_projections", "harborrag-transform"),
        ("harborrag.write_vector_projection", "harborrag-index"),
        ("harborrag.write_graph_projection", "harborrag-index"),
        ("harborrag.verify_projections", "harborrag-index"),
        ("harborrag.publish_version", "harborrag-index"),
    )


@pytest.mark.asyncio
async def test_batch_workflow_uses_bounded_document_child_windows(
    monkeypatch,
) -> None:
    active = 0
    maximum = 0
    indices = []

    async def child(name, request, **options):
        nonlocal active, maximum
        assert name == "harborrag.document_ingestion"
        active += 1
        maximum = max(maximum, active)
        indices.append(request.document_index)
        await asyncio.sleep(0)
        active -= 1
        return "published" if request.document_index < 2 else "unchanged"

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_workflow.workflow.execute_child_workflow",
        child,
    )

    result = await SourceBatchWorkflow().run(
        SourceBatchInput(
            task_id="task-1",
            tenant_id="tenant-1",
            connector_name="local-docs",
            plan_reference=_plan_reference(),
            start_index=0,
            end_index=3,
            batch_number=0,
            document_concurrency=2,
        )
    )

    assert result == DocumentDispatchSummary(published=2, unchanged=1)
    assert indices == [0, 1, 2]
    assert maximum == 2


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
        "harborrag_runtime.temporal.source_workflow.workflow.execute_child_workflow",
        child,
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
        "harborrag_runtime.temporal.source_workflow.workflow.execute_child_workflow",
        child,
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
        "harborrag_runtime.temporal.source_workflow.workflow.execute_child_workflow",
        child,
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
