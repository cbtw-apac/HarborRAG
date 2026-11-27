"""Temporal activities for projection cleanup, relation repair and reindexing.

These activities run outside the ingestion activity boundary, so their events carry
their own IDs and counts.
"""

from __future__ import annotations

import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError

from harborrag_core.observability.log_events import log_event
from harborrag_engine.ingestion.maintenance.cleanup import ProjectionCleanupBatch
from harborrag_engine.ingestion.maintenance.reindex import ReindexRequest
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.failure_classification import ingestion_failure_classifier
from harborrag_runtime.ingestion.maintenance.retention import RetiredVersionPurgeBatch
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
_MAINTENANCE_BATCH_LIMIT = 1_000
"""Most cleanup jobs or retired versions one activity attempt handles."""


class MaintenanceActivities:
    """Run retryable projection maintenance outside document stage activities.

    Args:
        runtime: Ingestion runtime whose services the activities call.
        telemetry: Ingestion metrics sink; a default one when omitted.
    """

    def __init__(
        self,
        runtime: IngestionRuntime,
        *,
        telemetry: IngestionTelemetry | None = None,
    ) -> None:
        self._runtime = runtime
        self._failures = ingestion_failure_classifier()
        self._telemetry = telemetry or IngestionTelemetry()

    @activity.defn(name="harborrag.cleanup_source_projections")
    async def cleanup_source_projections(
        self,
        source: SourceIngestionInput,
    ) -> ProjectionCleanupResult:
        """Clean the pending projections of a source scope and purge its retired versions.

        Args:
            source: The source run whose scope is cleaned.

        Returns:
            Counts of cleanup jobs and purged versions.

        Raises:
            ApplicationError: Some cleanup jobs failed.
        """
        with self._telemetry.stage(
            IngestionStage.CLEANUP,
            attempt=_activity_attempt(),
        ):
            cleanup = await self._runtime.cleanup.run_scope(
                tenant_id=source.tenant_id,
                source_scope_id=source.source_scope_id,
                limit=_MAINTENANCE_BATCH_LIMIT,
            )
            self._telemetry.record_cleanup_backlog(cleanup.failed)
            purge = await self._purge_retired_versions(source)
            return self._cleanup_result(cleanup, purge)

    async def _purge_retired_versions(
        self,
        source: SourceIngestionInput,
    ) -> RetiredVersionPurgeBatch:
        """Apply the retired-version retention TTL to the scope just cleaned.

        A version whose purge fails stays PURGING under its lease, and a later
        run resumes it once the lease lapses, so it is reported but does not
        fail the activity. A retry would only redo the cleanup for nothing.
        """
        retention = self._runtime.retention
        if retention is None:
            return RetiredVersionPurgeBatch()
        purge = await retention.run_scope(
            tenant_id=source.tenant_id,
            source_scope_id=source.source_scope_id,
            limit=_MAINTENANCE_BATCH_LIMIT,
        )
        self._telemetry.record_retention_purge(
            purged=purge.purged,
            skipped=purge.skipped,
            failed=purge.failed,
            deleted_objects=purge.deleted_objects,
        )
        if purge.failed:
            log_event(
                logger,
                "ingestion.retired_version_purge_incomplete",
                level=logging.WARNING,
                outcome="left_for_next_run",
                source_scope_id=source.source_scope_id,
                purged_count=purge.purged,
                failed_count=purge.failed,
            )
        return purge

    @activity.defn(name="harborrag.cleanup_reindex_projections")
    async def cleanup_reindex_projections(
        self,
        request: ReindexInput,
    ) -> ProjectionCleanupResult:
        """Clean the projections a reindex left pending.

        Args:
            request: The reindex whose tenant, or single document, is cleaned.

        Returns:
            Counts of cleanup jobs.

        Raises:
            ApplicationError: Some cleanup jobs failed.
        """
        with self._telemetry.stage(
            IngestionStage.CLEANUP,
            attempt=_activity_attempt(),
        ):
            if request.document_id is None:
                cleanup = await self._runtime.cleanup.run_pending(
                    tenant_id=request.tenant_id,
                    limit=min(request.limit, _MAINTENANCE_BATCH_LIMIT),
                )
            else:
                cleanup = await self._runtime.cleanup.run_documents(
                    tenant_id=request.tenant_id,
                    document_ids=(request.document_id,),
                    limit=_MAINTENANCE_BATCH_LIMIT,
                )
            self._telemetry.record_cleanup_backlog(cleanup.failed)
            return self._cleanup_result(cleanup)

    @activity.defn(name="harborrag.repair_reindex_relations")
    async def repair_reindex_relations(
        self,
        request: ReindexInput,
    ) -> RelationRepairResult:
        """Restore active source edges after a reindex."""
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
        purge: RetiredVersionPurgeBatch | None = None,
    ) -> ProjectionCleanupResult:
        """Return the cleanup's counts, failing the activity when a cleanup job failed."""
        purge = purge or RetiredVersionPurgeBatch()
        result = ProjectionCleanupResult(
            claimed=cleanup.claimed,
            completed=cleanup.completed,
            cancelled=cleanup.cancelled,
            failed=cleanup.failed,
            purged_versions=purge.purged,
            purge_failed_versions=purge.failed,
        )
        if result.failed:
            raise ApplicationError("projection cleanup batch contains failed jobs")
        return result

    @activity.defn(name="harborrag.reindex")
    async def reindex(
        self,
        request: ReindexInput,
    ) -> ReindexResult:
        """Run a connector-free reindex under heartbeats.

        Args:
            request: The reindex job, its tenant, processing profile and scope.

        Returns:
            The job's status and counts.

        Raises:
            ApplicationError: The reindex failed, retryable per its classification.
        """
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
        """Run the reindex job and return its counts, warning about isolated document failures."""
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
            log_event(
                logger,
                "ingestion.reindex_completed_with_failures",
                level=logging.WARNING,
                reindex_job_id=result.reindex_job_id,
                scanned_count=result.scanned_count,
                published_count=result.published_count,
                failure_count=result.failure_count,
                last_error_code=result.last_error_code,
            )
        return result


def _activity_attempt() -> int:
    """Return the running activity's attempt, or 1 outside an activity."""
    try:
        return activity.info().attempt
    except RuntimeError:
        return 1
