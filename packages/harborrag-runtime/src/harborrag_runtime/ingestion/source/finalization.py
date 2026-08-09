"""Authoritative source reconciliation and task finalization."""

from __future__ import annotations

import asyncio
import logging

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.ingestion import BindingKind, IngestionTaskState

from ..maintenance.relation_repair import GraphRelationRepairService, RelationRepairResult
from .models import (
    PlannedDocumentRelease,
    SourceDispatchSummary,
    SourceIngestionOutcome,
    SourceIngestionRequest,
)

logger = logging.getLogger("harborrag.runtime.ingestion.source_finalization")


class SourceFinalizationService:
    """Repair relations, reconcile removals, and close a source task."""

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
        planned: tuple[PlannedDocumentRelease, ...],
        summary: SourceDispatchSummary,
    ) -> SourceIngestionOutcome:
        summary.require_total(len(planned))
        logger.info(
            "Source finalization started task_id=%s scan_id=%s documents=%d",
            request.task_id,
            scan_id,
            len(planned),
        )
        await self._control.tasks.update_summary(
            request.task_id,
            {"stage": "RECONCILING"},
        )
        relation_repair = await self._repair_relations(request, planned, summary)
        removals = await self._reconcile_removals(
            request,
            scan_id=scan_id,
            planned=planned,
            summary=summary,
        )
        unresolved_relations = (
            relation_repair.unresolved_relations if relation_repair is not None else 0
        )
        await self._control.tasks.finalize(
            request.task_id,
            summary.task_state(),
            summary={
                "stage": "COMPLETED",
                "discovered": len(planned),
                "admitted": len(planned),
                "published": summary.published,
                "unchanged": summary.unchanged,
                "failed": summary.failed,
                "removal_candidates": len(removals),
                "unresolved_relations": unresolved_relations,
            },
        )
        outcome = SourceIngestionOutcome(
            task_id=request.task_id,
            scan_id=scan_id,
            discovered=len(planned),
            published=summary.published,
            unchanged=summary.unchanged,
            failed=summary.failed,
            status=summary.task_state(),
            removal_candidates=removals,
            unresolved_relations=unresolved_relations,
        )
        logger.info(
            "Source finalization completed task_id=%s status=%s removals=%d "
            "unresolved_relations=%d",
            request.task_id,
            outcome.status.value,
            len(removals),
            unresolved_relations,
        )
        return outcome

    async def _repair_relations(
        self,
        request: SourceIngestionRequest,
        planned: tuple[PlannedDocumentRelease, ...],
        summary: SourceDispatchSummary,
    ) -> RelationRepairResult | None:
        if self._relations is None:
            return None
        try:
            return await self._relations.repair(
                planned,
                tenant_id=request.tenant_id,
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

    async def _reconcile_removals(
        self,
        request: SourceIngestionRequest,
        *,
        scan_id: str,
        planned: tuple[PlannedDocumentRelease, ...],
        summary: SourceDispatchSummary,
    ) -> tuple[str, ...]:
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
            await asyncio.gather(
                *(
                    self._control.publisher.retire_removed(document_id=document_id)
                    for document_id in removals
                )
            )
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
        planned: tuple[PlannedDocumentRelease, ...],
        summary: SourceDispatchSummary,
        *,
        failed_stage: str,
    ) -> None:
        discovered = len(planned)
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
            },
        )
