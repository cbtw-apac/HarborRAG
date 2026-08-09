"""Cleanup of rebuildable document projections."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from harborrag_adapters.repositories.database import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.ingestion import ProjectionCleanupJob
from harborrag_core.ports import KnowledgeGraphRepositoryPort
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion import CleanupPolicy, VectorProjectionStore

logger = logging.getLogger("harborrag.runtime.ingestion.cleanup")


@dataclass(frozen=True, slots=True)
class ProjectionCleanupBatch:
    claimed: int
    completed: int
    cancelled: int
    failed: int


class ProjectionCleanupService:
    """Delete version-addressed projections without touching canonical evidence."""

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        vector_store: VectorProjectionStore,
        graph_store: KnowledgeGraphRepositoryPort,
    ) -> None:
        self._control = control
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._policy = CleanupPolicy()

    async def run_scope(
        self,
        *,
        tenant_id: str,
        source_scope_id: str,
        limit: int = 100,
    ) -> ProjectionCleanupBatch:
        jobs = await self._control.reliability.pending_cleanup_jobs(
            limit=limit,
            source_scope_id=source_scope_id,
        )
        return await self._run(jobs, tenant_id=tenant_id)

    async def run_documents(
        self,
        *,
        tenant_id: str,
        document_ids: Sequence[str],
        limit: int = 100,
    ) -> ProjectionCleanupBatch:
        jobs = await self._control.reliability.pending_cleanup_jobs(
            limit=limit,
            document_ids=document_ids,
        )
        return await self._run(jobs, tenant_id=tenant_id)

    async def run_pending(
        self,
        *,
        tenant_id: str,
        limit: int = 1_000,
    ) -> ProjectionCleanupBatch:
        """Drain unscoped jobs after a corpus-wide reindex."""

        jobs = await self._control.reliability.pending_cleanup_jobs(
            limit=limit,
        )
        return await self._run(jobs, tenant_id=tenant_id)

    async def _run(
        self,
        jobs: Sequence[ProjectionCleanupJob],
        *,
        tenant_id: str,
    ) -> ProjectionCleanupBatch:
        logger.info(
            "Projection cleanup batch started jobs=%d",
            len(jobs),
        )
        claimed = completed = cancelled = failed = 0
        context = StorageOperationContext.system(tenant_id)
        for job in jobs:
            if not await self._control.reliability.claim_cleanup(str(job.cleanup_job_id)):
                continue
            claimed += 1
            outcome = await self._clean(job, context=context)
            completed += outcome == "completed"
            cancelled += outcome == "cancelled"
            failed += outcome == "failed"
        batch = ProjectionCleanupBatch(
            claimed=claimed,
            completed=completed,
            cancelled=cancelled,
            failed=failed,
        )
        logger.info(
            "Projection cleanup batch completed jobs=%d claimed=%d completed=%d "
            "cancelled=%d failed=%d",
            len(jobs),
            batch.claimed,
            batch.completed,
            batch.cancelled,
            batch.failed,
        )
        return batch

    async def _clean(
        self,
        job: ProjectionCleanupJob,
        *,
        context: StorageOperationContext,
    ) -> str:
        cleanup_id = str(job.cleanup_job_id)
        version_id = str(job.document_version_id)
        try:
            active = await self._control.document_versions.active_versions([str(job.document_id)])
            active_version = active.get(str(job.document_id))
            active_version_id = (
                str(active_version.document_version_id) if active_version is not None else None
            )
            if not self._policy.may_delete(
                document_version_id=version_id,
                active_version_id=active_version_id,
            ):
                await self._control.reliability.cancel_cleanup(
                    cleanup_id,
                    safe_reason_code="document_version_is_active",
                )
                logger.info(
                    "Projection cleanup cancelled cleanup_id=%s document_id=%s "
                    "document_version_id=%s reason=document_version_is_active",
                    cleanup_id,
                    job.document_id,
                    version_id,
                )
                return "cancelled"
            manifest = await self._control.reliability.projection_manifest(version_id)
            if manifest is not None:
                await self._vector_store.delete_manifest(
                    manifest,
                    context=context,
                )
            await self._graph_store.delete_version(
                version_id,
                context=context,
            )
            await self._control.reliability.complete_cleanup(cleanup_id)
            logger.info(
                "Projection cleanup completed cleanup_id=%s document_id=%s document_version_id=%s",
                cleanup_id,
                job.document_id,
                version_id,
            )
            return "completed"
        except Exception as error:
            logger.error(
                "Projection cleanup failed cleanup_id=%s document_id=%s "
                "document_version_id=%s error_type=%s",
                cleanup_id,
                job.document_id,
                version_id,
                type(error).__name__,
            )
            await self._control.reliability.fail_cleanup(
                cleanup_id,
                safe_error_code=(f"projection_cleanup_{type(error).__name__.lower()}"),
            )
            return "failed"
