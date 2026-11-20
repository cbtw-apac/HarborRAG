"""Authoritative source reconciliation and task finalization.

Each terminal summary stored here replaces the task's summary, so each one
keeps the discovery skip counts that status views read after the run ends.
"""

from __future__ import annotations

import asyncio
import logging

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.ingestion import BindingKind, IngestionTaskState

from ..maintenance.relation_repair import GraphRelationRepairService, RelationRepairResult
from .models import (
    PlannedDocuments,
    RelationRepairProgress,
    SourceDispatchSummary,
    SourceIngestionOutcome,
    SourceIngestionRequest,
)
from .skips import carried_discovery_skips, run_skip_summary

logger = logging.getLogger("harborrag.runtime.ingestion.source_finalization")

_MAX_CONCURRENT_RETIREMENTS = 4


class SourceFinalizationService:
    """Repair relations, reconcile removals, and close a source task.

    Args:
        control: Control-plane database that holds the task, its scan and results.
        relations: Graph relation repair. Without it, finalization skips repair.
    """

    def __init__(
        self,
        *,
        control: IngestionControlPlaneDatabase,
        relations: GraphRelationRepairService | None = None,
    ) -> None:
        self._control = control
        self._relations = relations

    async def finish(
        self,
        request: SourceIngestionRequest,
        *,
        scan_id: str,
        planned: PlannedDocuments,
        summary: SourceDispatchSummary,
        repair_progress: RelationRepairProgress | None = None,
    ) -> SourceIngestionOutcome:
        """Repair relations, reconcile removals, and record the run's final outcome.

        The final summary replaces the task's summary and keeps the run's skip
        counts. If repair or reconciliation fails, the task is marked failed with
        the counts so far and the error is raised. A later attempt may finalize
        that task again.
        """
        summary.require_total(planned.document_count)
        logger.info(
            "Source finalization started task_id=%s scan_id=%s documents=%d",
            request.task_id,
            scan_id,
            planned.document_count,
        )
        await self._control.tasks.update_summary(
            request.task_id,
            {"stage": "RECONCILING"},
        )
        relation_repair = await self._repair_relations(
            request, planned, summary, repair_progress or RelationRepairProgress()
        )
        removals = await self._reconcile_removals(
            request,
            scan_id=scan_id,
            planned=planned,
            summary=summary,
        )
        unresolved_relations = (
            relation_repair.unresolved_relations if relation_repair is not None else 0
        )
        skips = await run_skip_summary(self._control, request.task_id)
        await self._control.tasks.finalize(
            request.task_id,
            summary.task_state(),
            summary={
                "stage": "COMPLETED",
                "discovered": planned.document_count,
                "admitted": planned.document_count,
                "published": summary.published,
                "unchanged": summary.unchanged,
                "failed": summary.failed,
                **skips,
                "removal_candidates": len(removals),
                "unresolved_relations": unresolved_relations,
            },
        )
        outcome = SourceIngestionOutcome(
            task_id=request.task_id,
            scan_id=scan_id,
            discovered=planned.document_count,
            published=summary.published,
            unchanged=summary.unchanged,
            failed=summary.failed,
            status=summary.task_state(),
            removal_candidates=removals,
            unresolved_relations=unresolved_relations,
            skipped=skips["skipped"],
            skipped_by_reason=skips["skipped_by_reason"],
        )
        logger.info(
            "Source finalization completed task_id=%s status=%s discovered=%d "
            "published=%d unchanged=%d failed=%d skipped=%s skipped_by_reason=%s "
            "removals=%d unresolved_relations=%d",
            request.task_id,
            outcome.status.value,
            planned.document_count,
            summary.published,
            summary.unchanged,
            summary.failed,
            skips["skipped"],
            skips["skipped_by_reason"],
            len(removals),
            unresolved_relations,
        )
        return outcome

    async def _repair_relations(
        self,
        request: SourceIngestionRequest,
        planned: PlannedDocuments,
        summary: SourceDispatchSummary,
        progress: RelationRepairProgress,
    ) -> RelationRepairResult | None:
        """Repair the plan's relations page by page, then the reverse direction once.

        Every document is already published, so no page's repair depends on
        another's, and memory stays bounded by one page. ``progress`` is updated
        in place after each page, and the Temporal activity heartbeats it so a
        retry resumes there. The reverse pass repairs documents outside the plan
        whose links point at something now published. It is idempotent, so a
        retry runs it again instead of resuming totals it would count twice. The
        result keeps the plan's own unresolved count, because the reverse pass
        only reduces what is unresolved and its declarers belong to other runs.
        """
        if self._relations is None:
            return None
        try:
            async for page in planned.pages(progress.next_page):
                result = await self._relations.repair(page, tenant_id=request.tenant_id)
                progress.repaired_documents += result.repaired_documents
                progress.resolved_relations += result.resolved_relations
                progress.unresolved_relations += result.unresolved_relations
                progress.next_page += 1
            reverse = await self._relations.repair_resolvable(
                tenant_id=request.tenant_id,
                graph_projection_version=request.processing.graph_projection_version,
            )
            return RelationRepairResult(
                repaired_documents=progress.repaired_documents + reverse.repaired_documents,
                resolved_relations=progress.resolved_relations + reverse.resolved_relations,
                unresolved_relations=progress.unresolved_relations,
            )
        except Exception as error:
            logger.error(
                "Source relation repair failed task_id=%s error_type=%s",
                request.task_id,
                type(error).__name__,
            )
            await self._record_failure(
                request.task_id,
                planned,
                summary,
                failed_stage="relation_repair",
            )
            raise

    async def _retire_all(self, removals: tuple[str, ...]) -> None:
        """Retire every removed document without exhausting the connection pool.

        ``retire_removed`` opens a session per call, so gathering one task per
        removal put the whole batch in flight at once. A scan that detected a
        few hundred removals overran a pool sized ``control_db_pool_size +
        control_db_max_overflow`` (15 by default), timed out, and marked the
        task FAILED even though every document had already been published.
        At most ``_MAX_CONCURRENT_RETIREMENTS`` run at once, comfortably under the
        smallest supported pool, so retirement never competes with the rest of
        the task for connections.
        """
        slots = asyncio.Semaphore(_MAX_CONCURRENT_RETIREMENTS)

        async def retire(document_id: str) -> None:
            async with slots:
                await self._control.publisher.retire_removed(document_id=document_id)

        await asyncio.gather(*(retire(document_id) for document_id in removals))

    async def _reconcile_removals(
        self,
        request: SourceIngestionRequest,
        *,
        scan_id: str,
        planned: PlannedDocuments,
        summary: SourceDispatchSummary,
    ) -> tuple[str, ...]:
        if _is_partial_scan(request):
            logger.info(
                "Skipping removal reconciliation for a partial scan task_id=%s "
                "(limit/updated_after set)",
                request.task_id,
            )
            return ()
        try:
            removals = await self._control.source_scans.reconcile_removals(
                scan_id,
                missing_threshold=request.missing_threshold,
                immediate_binding_kinds=(
                    {BindingKind.ATTACHMENT.value}
                    if not request.query.include_attachments
                    else frozenset()
                ),
            )
            await self._retire_all(removals)
            logger.info(
                "Source removal reconciliation completed task_id=%s removals=%d",
                request.task_id,
                len(removals),
            )
            return removals
        except Exception as error:
            logger.error(
                "Source removal reconciliation failed task_id=%s error_type=%s",
                request.task_id,
                type(error).__name__,
            )
            await self._record_failure(
                request.task_id,
                planned,
                summary,
                failed_stage="removal_reconciliation",
            )
            raise

    async def _record_failure(
        self,
        task_id: str,
        planned: PlannedDocuments,
        summary: SourceDispatchSummary,
        *,
        failed_stage: str,
    ) -> None:
        """Mark the task failed at a finalization stage, keeping its counts so far.

        A later attempt may finalize the task again, and reads the discovery skip
        counts kept here.
        """
        task = await self._control.tasks.get(task_id)
        carried = carried_discovery_skips(task.summary) if task is not None else {}
        discovered = planned.document_count
        await self._control.tasks.transition(
            task_id,
            IngestionTaskState.FAILED,
            summary={
                "stage": "COMPLETED",
                "failed_stage": failed_stage,
                "discovered": discovered,
                "admitted": discovered,
                "published": summary.published,
                "unchanged": summary.unchanged,
                "failed": summary.failed,
                **carried,
            },
        )


def _is_partial_scan(request: SourceIngestionRequest) -> bool:
    """Whether a limit or ``updated_after`` narrowed the scan.

    A deliberately partial scan cannot tell "not in scope" from "gone", so it
    never reconciles removals.
    """
    return request.query.limit is not None or request.query.updated_after is not None
