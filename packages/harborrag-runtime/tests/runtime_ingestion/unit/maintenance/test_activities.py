"""Maintenance activity boundary behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import ApplicationError

from harborrag_core.ingestion import ReindexJob, ReindexJobState
from harborrag_runtime.ingestion.maintenance.cleanup import ProjectionCleanupBatch
from harborrag_runtime.ingestion.maintenance.relation_repair import RelationRepairResult
from harborrag_runtime.temporal.maintenance_activities import (
    MaintenanceActivities,
)
from harborrag_runtime.temporal.maintenance_schemas import (
    ReindexInput,
)
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
)


def _processing() -> ProcessingProfileInput:
    return ProcessingProfileInput(
        parser_profile="parser-v1",
        normalizer_version="canonical-v1",
        chunk_strategy="chunks-v1",
        dense_encoder_profile="dense-v1",
        sparse_encoder_profile="sparse-v1",
        graph_projection_version="graph-v1",
    )


def _source() -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id="task-1",
        tenant_id="tenant-1",
        connector_name="local-docs",
        connector_type="local",
        connection_id="local-docs",
        source_scope_id="docs",
        configuration_fingerprint="config-v1",
        processing=_processing(),
    )


def _reindex(*, document_id: str | None = None) -> ReindexInput:
    return ReindexInput(
        reindex_job_id="reindex-1",
        tenant_id="tenant-1",
        processing=_processing(),
        document_id=document_id,
        limit=17,
    )


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        cleanup=SimpleNamespace(
            run_scope=AsyncMock(
                return_value=ProjectionCleanupBatch(
                    claimed=1,
                    completed=1,
                    cancelled=0,
                    failed=0,
                )
            ),
            run_pending=AsyncMock(
                return_value=ProjectionCleanupBatch(
                    claimed=0,
                    completed=0,
                    cancelled=0,
                    failed=0,
                )
            ),
            run_documents=AsyncMock(
                return_value=ProjectionCleanupBatch(
                    claimed=1,
                    completed=0,
                    cancelled=1,
                    failed=0,
                )
            ),
        ),
        relations=SimpleNamespace(
            repair_reindexed=AsyncMock(
                return_value=RelationRepairResult(
                    repaired_documents=2,
                    resolved_relations=3,
                    unresolved_relations=1,
                )
            )
        ),
        reindex=SimpleNamespace(
            run=AsyncMock(
                return_value=ReindexJob(
                    reindex_job_id="reindex-1",
                    status=ReindexJobState.COMPLETED,
                    target_processing_fingerprint="processing-v1",
                    scanned_count=1,
                    processed_count=1,
                    published_count=1,
                )
            )
        ),
    )


@pytest.mark.asyncio
async def test_maintenance_cleanup_routes_source_corpus_and_document_scopes() -> None:
    runtime = _runtime()
    activities = MaintenanceActivities(runtime)

    source = await activities.cleanup_source_projections(_source())
    corpus = await activities.cleanup_reindex_projections(_reindex())
    document = await activities.cleanup_reindex_projections(_reindex(document_id="document-1"))

    assert (source.completed, corpus.claimed, document.cancelled) == (1, 0, 1)
    runtime.cleanup.run_scope.assert_awaited_once_with(
        tenant_id="tenant-1",
        source_scope_id="docs",
        limit=1_000,
    )
    runtime.cleanup.run_pending.assert_awaited_once_with(
        tenant_id="tenant-1",
        limit=17,
    )
    runtime.cleanup.run_documents.assert_awaited_once_with(
        tenant_id="tenant-1",
        document_ids=("document-1",),
        limit=1_000,
    )


@pytest.mark.asyncio
async def test_reindex_cleanup_caps_corpus_batch_at_repository_limit() -> None:
    runtime = _runtime()

    await MaintenanceActivities(runtime).cleanup_reindex_projections(
        ReindexInput(
            reindex_job_id="reindex-large",
            tenant_id="tenant-1",
            processing=_processing(),
            limit=10_000,
        )
    )

    runtime.cleanup.run_pending.assert_awaited_once_with(
        tenant_id="tenant-1",
        limit=1_000,
    )


def test_maintenance_cleanup_rejects_a_failed_batch() -> None:
    with pytest.raises(ApplicationError, match="contains failed jobs"):
        MaintenanceActivities._cleanup_result(
            ProjectionCleanupBatch(
                claimed=1,
                completed=0,
                cancelled=0,
                failed=1,
            )
        )


@pytest.mark.asyncio
async def test_maintenance_repairs_reindex_relations_with_typed_profile() -> None:
    runtime = _runtime()
    result = await MaintenanceActivities(runtime).repair_reindex_relations(
        _reindex(document_id="document-1")
    )

    assert (
        result.repaired_documents,
        result.resolved_relations,
        result.unresolved_relations,
    ) == (2, 3, 1)
    request = runtime.relations.repair_reindexed.await_args.kwargs
    assert request["tenant_id"] == "tenant-1"
    assert request["anchor_document_id"] == "document-1"
    assert request["processing"].graph_projection_version == "graph-v1"


@pytest.mark.asyncio
async def test_maintenance_reindex_maps_success_and_durable_failure() -> None:
    runtime = _runtime()
    activities = MaintenanceActivities(runtime)

    result = await activities.reindex(_reindex(document_id="document-1"))

    assert result.status == "COMPLETED"
    assert result.connector_call_count == 0
    request = runtime.reindex.run.await_args.args[0]
    assert request.document_id == "document-1"
    assert request.limit == 17

    runtime.reindex.run.return_value = ReindexJob(
        reindex_job_id="reindex-1",
        status=ReindexJobState.FAILED,
        target_processing_fingerprint="processing-v1",
        scanned_count=1,
        processed_count=1,
        failure_count=1,
        last_error_code="graph-write-failed",
    )
    failed = await activities.reindex(_reindex())
    assert failed.status == "FAILED"
    assert failed.failure_count == 1
    assert failed.last_error_code == "graph-write-failed"


@pytest.mark.asyncio
async def test_maintenance_reindex_sanitizes_provider_errors_and_preserves_declared_errors() -> (
    None
):
    runtime = _runtime()
    activities = MaintenanceActivities(runtime)
    runtime.reindex.run.side_effect = ConnectionError("private endpoint")

    with pytest.raises(ApplicationError, match="inspect restricted worker logs") as captured:
        await activities.reindex(_reindex())
    assert "private endpoint" not in str(captured.value)

    declared = ApplicationError("declared-safe-error", non_retryable=True)
    runtime.reindex.run.side_effect = declared
    with pytest.raises(ApplicationError) as preserved:
        await activities.reindex(_reindex())
    assert preserved.value is declared
