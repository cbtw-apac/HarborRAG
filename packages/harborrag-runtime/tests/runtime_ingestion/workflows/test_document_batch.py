"""Document and bounded source-batch Temporal workflow behavior."""

from __future__ import annotations

import asyncio

import pytest

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.temporal.document_workflow import DocumentIngestionWorkflow
from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
    SourceBatchInput,
)
from harborrag_runtime.temporal.source_batch_workflow import SourceBatchWorkflow
from harborrag_runtime.temporal_models import TaskQueueConfig, TemporalWorkflowOptions

from .fixtures import plan_reference as _plan_reference


@pytest.mark.asyncio
async def test_document_workflow_routes_twelve_stages_to_resource_queues(monkeypatch) -> None:
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
            return DocumentIngestionOutcome.PUBLISHED
        return request

    monkeypatch.setattr(
        "harborrag_runtime.temporal.document_workflow.workflow.execute_activity",
        execute_activity,
    )
    queues = TaskQueueConfig(
        discovery="test-discovery",
        transform="test-transform",
        io="test-io",
        parser="test-parser",
        model="test-model",
        index="test-index",
    )
    request = DocumentIngestionInput(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="local-docs",
        plan_reference=_plan_reference(),
        document_index=4,
        workflow_options=TemporalWorkflowOptions(task_queues=queues),
    )

    result = await DocumentIngestionWorkflow().run(request)

    assert result is DocumentIngestionOutcome.PUBLISHED
    assert tuple((call[0], call[2]["task_queue"]) for call in calls) == (
        ("harborrag.fetch_and_capture_raw", "test-io"),
        ("harborrag.parse_and_normalize", "test-parser"),
        ("harborrag.sync_content_units", "test-transform"),
        ("harborrag.persist_canonical", "test-io"),
        ("harborrag.chunk_and_validate", "test-transform"),
        ("harborrag.encode_chunks", "test-model"),
        ("harborrag.build_relations", "test-transform"),
        ("harborrag.build_projections", "test-transform"),
        ("harborrag.write_vector_projection", "test-index"),
        ("harborrag.write_graph_projection", "test-index"),
        ("harborrag.verify_projections", "test-index"),
        ("harborrag.publish_version", "test-index"),
    )


@pytest.mark.asyncio
async def test_batch_workflow_uses_bounded_document_child_windows(monkeypatch) -> None:
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
        return (
            DocumentIngestionOutcome.PUBLISHED
            if request.document_index < 2
            else DocumentIngestionOutcome.UNCHANGED
        )

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.execute_child_workflow",
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


def test_document_dispatch_summary_rejects_unknown_outcomes_and_negative_counts() -> None:
    with pytest.raises(ValueError, match="unsupported document ingestion outcome"):
        DocumentDispatchSummary().add("cancelled")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must not be negative"):
        DocumentDispatchSummary(failed=-1)
