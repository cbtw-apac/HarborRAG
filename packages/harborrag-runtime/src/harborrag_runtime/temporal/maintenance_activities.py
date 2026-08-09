from __future__ import annotations

import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError

from harborrag_engine.ingestion import IngestionFailureClassifier
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.maintenance.cleanup import ProjectionCleanupBatch
from harborrag_runtime.ingestion.maintenance.reindex import ReindexRequest
from harborrag_runtime.ingestion.observability import (
    IngestionStage,
    IngestionTelemetry,
)

from .conversion import to_processing_profile
from .heartbeats import heartbeat_while
from .maintenance_schemas import (
    ProjectionCleanupResult,
    ReindexInput,
    ReindexResult,
    RelationRepairResult,
)
from .schemas import (
    SourceIngestionInput,
)

logger = logging.getLogger("harborrag.runtime.temporal.maintenance")


class MaintenanceActivities:
    """Run retryable projection maintenance outside document stage activities."""

    def __init__(
        self,
        runtime: IngestionRuntime,
        *,
        telemetry: IngestionTelemetry | None = None,
    ) -> None:
        self._runtime = runtime
        self._failures = IngestionFailureClassifier()
        self._telemetry = telemetry or IngestionTelemetry()

    @activity.defn(name="harborrag.cleanup_source_projections")
    async def cleanup_source_projections(
        self,
        source: SourceIngestionInput,
    ) -> ProjectionCleanupResult:
        with self._telemetry.stage(
            IngestionStage.CLEANUP,
            attempt=_activity_attempt(),
        ):
            cleanup = await self._runtime.cleanup.run_scope(
                tenant_id=source.tenant_id,
                source_scope_id=source.source_scope_id,
                limit=1_000,
            )
            self._telemetry.record_cleanup_backlog(cleanup.failed)
            return self._cleanup_result(cleanup)

    @activity.defn(name="harborrag.cleanup_reindex_projections")
    async def cleanup_reindex_projections(
        self,
        request: ReindexInput,
    ) -> ProjectionCleanupResult:
        with self._telemetry.stage(
            IngestionStage.CLEANUP,
            attempt=_activity_attempt(),
        ):
            if request.document_id is None:
                cleanup = await self._runtime.cleanup.run_pending(
                    tenant_id=request.tenant_id,
                    limit=min(request.limit, 1_000),
                )
            else:
                cleanup = await self._runtime.cleanup.run_documents(
                    tenant_id=request.tenant_id,
                    document_ids=(request.document_id,),
                    limit=1_000,
                )
            self._telemetry.record_cleanup_backlog(cleanup.failed)
            return self._cleanup_result(cleanup)

    @activity.defn(name="harborrag.repair_reindex_relations")
    async def repair_reindex_relations(
        self,
        request: ReindexInput,
    ) -> RelationRepairResult:
        with self._telemetry.stage(
            IngestionStage.RELATION_REPAIR,
            attempt=_activity_attempt(),
        ):
            result = await self._runtime.relations.repair_reindexed(
                tenant_id=request.tenant_id,
                processing=to_processing_profile(request.processing),
                anchor_document_id=request.document_id,
            )
            return RelationRepairResult(
                repaired_documents=result.repaired_documents,
                resolved_relations=result.resolved_relations,
                unresolved_relations=result.unresolved_relations,
            )

    @staticmethod
    def _cleanup_result(
        cleanup: ProjectionCleanupBatch,
    ) -> ProjectionCleanupResult:
        result = ProjectionCleanupResult(
            claimed=cleanup.claimed,
            completed=cleanup.completed,
            cancelled=cleanup.cancelled,
            failed=cleanup.failed,
        )
        if result.failed:
            raise ApplicationError("projection cleanup batch contains failed jobs")
        return result

    @activity.defn(name="harborrag.reindex")
    async def reindex(
        self,
        request: ReindexInput,
    ) -> ReindexResult:
        with self._telemetry.stage(
            IngestionStage.REINDEX,
            attempt=_activity_attempt(),
        ):
            try:
                return await heartbeat_while(
                    self._run_reindex(request),
                    detail="reindex",
                )
            except ApplicationError:
                raise
            except Exception as error:
                failure = self._failures.classify("Reindex", error)
                raise ApplicationError(
                    "ingestion activity failed; inspect restricted worker logs",
                    type=failure.code,
                    non_retryable=not failure.retryable,
                ) from error

    async def _run_reindex(
        self,
        request: ReindexInput,
    ) -> ReindexResult:
        job = await self._runtime.reindex.run(
            ReindexRequest(
                reindex_job_id=request.reindex_job_id,
                tenant_id=request.tenant_id,
                processing=to_processing_profile(request.processing),
                document_id=request.document_id,
                limit=request.limit,
            )
        )
        result = ReindexResult(
            reindex_job_id=job.reindex_job_id,
            status=job.status.value,
            connector_call_count=job.connector_call_count,
            scanned_count=job.scanned_count,
            processed_count=job.processed_count,
            published_count=job.published_count,
            skipped_count=job.skipped_count,
            failure_count=job.failure_count,
            last_error_code=job.last_error_code,
        )
        if result.failure_count:
            logger.warning(
                "Connector-free reindex completed with isolated document failures",
                extra={
                    "reindex_job_id": result.reindex_job_id,
                    "scanned_count": result.scanned_count,
                    "published_count": result.published_count,
                    "failure_count": result.failure_count,
                    "last_error_code": result.last_error_code,
                },
            )
        return result


def _activity_attempt() -> int:
    try:
        return activity.info().attempt
    except RuntimeError:
        return 1
