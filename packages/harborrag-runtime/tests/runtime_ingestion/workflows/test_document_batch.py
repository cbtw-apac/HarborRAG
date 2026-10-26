"""Document and bounded source-batch Temporal workflow behavior."""

from __future__ import annotations

import asyncio

import pytest
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

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


@pytest.mark.asyncio
async def test_batch_workflow_starts_the_next_document_while_a_slow_one_is_still_running(
    monkeypatch,
) -> None:
    # With waves, document 2 waited for the whole first wave, slow document 0
    # included; the window starts it the moment document 1 frees a slot.
    slow_release = asyncio.Event()
    started: list[int] = []

    async def child(name, request, **options):
        del name, options
        started.append(request.document_index)
        if request.document_index == 0:
            await slow_release.wait()
        return DocumentIngestionOutcome.PUBLISHED

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.execute_child_workflow",
        child,
    )
    run = asyncio.create_task(
        SourceBatchWorkflow().run(
            SourceBatchInput(
                task_id="task-1",
                tenant_id="tenant-1",
                connector_name="local-docs",
                plan_reference=_plan_reference(),
                start_index=0,
                end_index=4,
                batch_number=0,
                document_concurrency=2,
            )
        )
    )
    for _ in range(50):
        await asyncio.sleep(0)

    assert started == [0, 1, 2, 3]
    assert not run.done()
    slow_release.set()
    assert await run == DocumentDispatchSummary(published=4)


def _batch_input(end_index: int, *, document_concurrency: int = 1) -> SourceBatchInput:
    return SourceBatchInput(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="local-docs",
        plan_reference=_plan_reference(),
        start_index=0,
        end_index=end_index,
        batch_number=0,
        document_concurrency=document_concurrency,
    )


@pytest.mark.asyncio
async def test_batch_pauses_after_an_unbroken_run_of_failures_and_resumes(monkeypatch) -> None:
    started: list[int] = []
    outage = True

    async def child(name, request, **options):
        del name, options
        started.append(request.document_index)
        return DocumentIngestionOutcome.FAILED if outage else DocumentIngestionOutcome.PUBLISHED

    async def wait_condition(predicate):
        # A real wait_condition needs a Temporal workflow event loop.
        while not predicate():
            await asyncio.sleep(0)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.execute_child_workflow",
        child,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.wait_condition",
        wait_condition,
    )
    batch = SourceBatchWorkflow()
    run = asyncio.create_task(batch.run(_batch_input(40)))
    for _ in range(200):
        await asyncio.sleep(0)

    # The 25th consecutive failure trips the breaker; nothing more is dispatched.
    assert started == list(range(25))
    assert not run.done()

    outage = False
    batch.resume()
    assert await run == DocumentDispatchSummary(published=15, failed=25)
    assert started == list(range(40))


@pytest.mark.asyncio
async def test_batch_failure_streak_resets_on_any_success(monkeypatch) -> None:
    async def child(name, request, **options):
        del name, options
        # Every 20th document succeeds, so no streak ever reaches the threshold.
        if request.document_index % 20 == 19:
            return DocumentIngestionOutcome.PUBLISHED
        return DocumentIngestionOutcome.FAILED

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.execute_child_workflow",
        child,
    )
    result = await SourceBatchWorkflow().run(_batch_input(60))

    assert result == DocumentDispatchSummary(published=3, failed=57)


@pytest.mark.asyncio
async def test_batch_breaker_ignores_missing_source_items(monkeypatch) -> None:
    async def child(name, request, **options):
        del name, options
        # A long run of deleted Jira items must not pause the source.
        return DocumentIngestionOutcome.SOURCE_ITEM_MISSING

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.execute_child_workflow",
        child,
    )
    result = await SourceBatchWorkflow().run(_batch_input(60))

    assert result == DocumentDispatchSummary(failed=60)


@pytest.mark.asyncio
async def test_batch_failure_streak_survives_missing_source_items(monkeypatch) -> None:
    started: list[int] = []

    async def child(name, request, **options):
        del name, options
        started.append(request.document_index)
        # Missing items interleaved with an outage neither count nor reset the streak.
        if request.document_index % 2:
            return DocumentIngestionOutcome.SOURCE_ITEM_MISSING
        return DocumentIngestionOutcome.FAILED

    async def wait_condition(predicate):
        while not predicate():
            await asyncio.sleep(0)

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.execute_child_workflow",
        child,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.wait_condition",
        wait_condition,
    )
    batch = SourceBatchWorkflow()
    run = asyncio.create_task(batch.run(_batch_input(80)))
    for _ in range(200):
        await asyncio.sleep(0)

    assert started == list(range(49))
    assert not run.done()
    batch.request_graceful_cancel()
    assert await run == DocumentDispatchSummary(failed=49)


@pytest.mark.asyncio
async def test_document_workflow_reports_a_missing_source_item(monkeypatch) -> None:
    async def execute_activity(name, request, **options):
        del request, options
        if name == "harborrag.fetch_and_capture_raw":
            raise ActivityError(
                "activity failed",
                scheduled_event_id=1,
                started_event_id=2,
                identity="worker",
                activity_type=name,
                activity_id="1",
                retry_state=None,
            ) from ApplicationError("not found", type="source_item_not_found")
        return None

    monkeypatch.setattr(
        "harborrag_runtime.temporal.document_workflow.workflow.execute_activity",
        execute_activity,
    )
    request = DocumentIngestionInput(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="jira",
        plan_reference=_plan_reference(),
        document_index=0,
        workflow_options=TemporalWorkflowOptions(),
    )

    result = await DocumentIngestionWorkflow().run(request)

    assert result is DocumentIngestionOutcome.SOURCE_ITEM_MISSING


@pytest.mark.asyncio
async def test_batch_counts_a_failed_document_workflow_instead_of_failing(monkeypatch) -> None:
    async def child(name, request, **options):
        del name, options
        if request.document_index == 1:
            raise ChildWorkflowError(
                "child failed",
                namespace="harborrag",
                workflow_id="harborrag-document:task-1:0:1",
                run_id="run-1",
                workflow_type="harborrag.document_ingestion",
                initiated_event_id=1,
                started_event_id=2,
                retry_state=None,
            )
        return DocumentIngestionOutcome.PUBLISHED

    monkeypatch.setattr(
        "harborrag_runtime.temporal.source_batch_workflow.workflow.execute_child_workflow",
        child,
    )
    result = await SourceBatchWorkflow().run(_batch_input(3, document_concurrency=2))

    assert result == DocumentDispatchSummary(published=2, failed=1)


def test_document_dispatch_summary_rejects_unknown_outcomes_and_negative_counts() -> None:
    with pytest.raises(ValueError, match="unsupported document ingestion outcome"):
        DocumentDispatchSummary().add("cancelled")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must not be negative"):
        DocumentDispatchSummary(failed=-1)
