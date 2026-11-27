"""Authoritative source reconciliation and task finalization.

The terminal summary stored here replaces the task's summary, so it keeps the
discovery skip counts that status views read after the run ends and the
progress bridge's state. A failed attempt never ends the task: its executor
retries it or marks the task failed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.ingestion import BindingKind
from harborrag_core.observability.log_events import FailureKind, log_event, log_failure
from harborrag_engine.ingestion.source.models import (
    PlannedDocuments,
    RelationRepairProgress,
    SourceDispatchSummary,
    SourceIngestionOutcome,
    SourceIngestionRequest,
)

from ..maintenance.relation_repair import GraphRelationRepairService, RelationRepairResult
from .skips import carried_progress_state, run_skip_summary

logger = logging.getLogger("harborrag.runtime.ingestion.source_finalization")

_MAX_CONCURRENT_RETIREMENTS = 4
"""Comfortably under the smallest supported control-plane pool, so retirement never competes with
the rest of the task for connections.
"""


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
        counts. If repair or reconciliation fails, the failed stage and the counts
        so far are merged into the task's summary and the error is raised. The
        task stays running, so a later attempt may finalize it.

        Args:
            request: The source run being finalized.
            scan_id: The run's authoritative scan.
            planned: The run's dispatch plan, read one page at a time.
            summary: Outcome counts of the dispatched documents; must cover the plan.
            repair_progress: Relation repair progress of an earlier attempt to resume from.

        Returns:
            The run's final outcome.
        """
        summary.require_total(planned.document_count)
        log_event(
            logger,
            "ingestion.finalization_started",
            task_id=request.task_id,
            scan_id=scan_id,
            documents_count=planned.document_count,
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
        task = await self._control.tasks.get(request.task_id)
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
                **(carried_progress_state(task.summary) if task is not None else {}),
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
        log_event(
            logger,
            "ingestion.finalization_completed",
            task_id=request.task_id,
            status=outcome.status.value,
            discovered_count=planned.document_count,
            published_count=summary.published,
            unchanged_count=summary.unchanged,
            failed_count=summary.failed,
            skipped_count=skips["skipped"],
            skipped_reason=_reason_counts(skips["skipped_by_reason"]),
            removals_count=len(removals),
            unresolved_relations_count=unresolved_relations,
        )
        return outcome

    async def _repair_relations(
        self,
        request: SourceIngestionRequest,
        planned: PlannedDocuments,
        summary: SourceDispatchSummary,
        progress: RelationRepairProgress,
    ) -> RelationRepairResult | None:
        """Repair relations that point at documents this run published.

        Repairs page by page: every document is already published, so no repair depends on another
        page's, and memory stays bounded by one page. ``progress`` is updated in place after each
        page; the caller may persist it (the Temporal activity heartbeats it) to resume there. A
        reverse pass then covers documents outside this plan whose links point at something this
        run, or an earlier one, has now published. It is not folded into ``progress``: it is
        idempotent, so a retried attempt re-runs it rather than resuming totals it would count
        twice. The result keeps the plan's own unresolved count, because the reverse pass only
        reduces it and its declarers belong to other runs' summaries.
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
            log_failure(
                logger,
                "ingestion.relation_repair_failed",
                error,
                FailureKind.CLASSIFIED,
                task_id=request.task_id,
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

        ``retire_removed`` opens a session per call, and the pool holds
        ``control_db_pool_size + control_db_max_overflow`` sessions (15 by default).
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
            log_event(
                logger,
                "ingestion.removal_reconciliation_skipped",
                task_id=request.task_id,
                reason="partial_scan",
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
            log_event(
                logger,
                "ingestion.removal_reconciliation_completed",
                task_id=request.task_id,
                removals_count=len(removals),
            )
            return removals
        except Exception as error:
            log_failure(
                logger,
                "ingestion.removal_reconciliation_failed",
                error,
                FailureKind.CLASSIFIED,
                task_id=request.task_id,
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
        """Record the failed finalization stage and the counts so far on the task.

        Only the summary changes. Temporal retries the attempt, and the run's
        executor marks the task failed once it gives up, keeping what is stored here.
        """
        discovered = planned.document_count
        await self._control.tasks.update_summary(
            task_id,
            {
                "failed_stage": failed_stage,
                "discovered": discovered,
                "admitted": discovered,
                "published": summary.published,
                "unchanged": summary.unchanged,
                "failed": summary.failed,
            },
        )


def _is_partial_scan(request: SourceIngestionRequest) -> bool:
    """Return whether a limit or ``updated_after`` narrowed the scan.

    A deliberately partial scan cannot tell "not in scope" from "gone", so it
    never reconciles removals.
    """
    return request.query.limit is not None or request.query.updated_after is not None


def _reason_counts(counts: Mapping[str, int]) -> str:
    """Render skip counts as comma-separated ``reason:count`` pairs for a log field."""
    return ",".join(f"{reason}:{count}" for reason, count in sorted(counts.items()))
