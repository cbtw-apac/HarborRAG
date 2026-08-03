"""Connector-free reindex workflow behavior."""

from __future__ import annotations

from dataclasses import replace

import pytest
from temporalio.converter import DataConverter

from harborrag_runtime.temporal.maintenance_schemas import (
    ProjectionCleanupResult,
    ReindexInput,
    ReindexResult,
    RelationRepairResult,
)
from harborrag_runtime.temporal.reindex_workflow import ReindexWorkflow
from harborrag_runtime.temporal.schemas import (
    DocumentDispatchSummary,
    DocumentIngestionInput,
    SourceBatchInput,
    SourceDiscoveryResult,
    SourceFinalizationInput,
    SourceIngestionInput,
)

from .fixtures import (
    plan_reference as _plan_reference,
)
from .fixtures import (
    source_input as _source,
)


def test_source_task_identity_matches_postgres_limit() -> None:
    with pytest.raises(ValueError, match="128"):
        replace(_source(), task_id="x" * 129)


@pytest.mark.asyncio
async def test_contracts_round_trip_through_temporal_converter() -> None:
    source = _source()
    reference = _plan_reference()
    summary = DocumentDispatchSummary(published=2, unchanged=1)
    values = [
        source,
        SourceDiscoveryResult(
            scan_id="scan-1",
            plan_reference=reference,
            document_count=3,
        ),
        SourceBatchInput(
            task_id="task-1",
            tenant_id="tenant-1",
            connector_name="local-docs",
            plan_reference=reference,
            start_index=0,
            end_index=3,
            batch_number=0,
            document_concurrency=2,
        ),
        DocumentIngestionInput(
            task_id="task-1",
            tenant_id="tenant-1",
            connector_name="local-docs",
            plan_reference=reference,
            document_index=1,
        ),
        SourceFinalizationInput(
            source=source,
            scan_id="scan-1",
            plan_reference=reference,
            summary=summary,
        ),
        ReindexInput(
            reindex_job_id="reindex-1",
            tenant_id="tenant-1",
            processing=source.processing,
            document_id="document-1",
        ),
    ]
    types = [
        SourceIngestionInput,
        SourceDiscoveryResult,
        SourceBatchInput,
        DocumentIngestionInput,
        SourceFinalizationInput,
        ReindexInput,
    ]

    payloads = await DataConverter.default.encode(values)
    decoded = await DataConverter.default.decode(payloads, types)

    assert decoded == values


@pytest.mark.asyncio
async def test_reindex_workflow_routes_connector_free_job_to_index_queue(
    monkeypatch,
) -> None:
    calls = []
    result = ReindexResult(
        reindex_job_id="reindex-1",
        status="COMPLETED",
        connector_call_count=0,
        scanned_count=1,
        processed_count=1,
        published_count=1,
        skipped_count=0,
        failure_count=0,
    )

    async def execute_activity(name, request, **options):
        calls.append((name, request, options))
        if name == "harborrag.cleanup_reindex_projections":
            return ProjectionCleanupResult(
                claimed=1,
                completed=1,
                cancelled=0,
                failed=0,
            )
        if name == "harborrag.repair_reindex_relations":
            return RelationRepairResult(
                repaired_documents=1,
                resolved_relations=2,
                unresolved_relations=0,
            )
        return result

    monkeypatch.setattr(
        "harborrag_runtime.temporal.reindex_workflow.workflow.execute_activity",
        execute_activity,
    )
    monkeypatch.setattr(
        "harborrag_runtime.temporal.reindex_workflow.workflow.patched",
        lambda _patch_id: True,
    )
    request = ReindexInput(
        reindex_job_id="reindex-1",
        tenant_id="tenant-1",
        processing=_source().processing,
    )

    assert await ReindexWorkflow().run(request) == result
    assert calls[0][0] == "harborrag.reindex"
    assert calls[0][2]["task_queue"] == "harborrag-index"
    assert calls[1][0] == "harborrag.cleanup_reindex_projections"
    assert calls[1][2]["task_queue"] == "harborrag-index"
    assert calls[2][0] == "harborrag.repair_reindex_relations"
    assert calls[2][2]["task_queue"] == "harborrag-index"
