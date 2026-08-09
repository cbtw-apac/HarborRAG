from __future__ import annotations

from datetime import timedelta
from typing import cast

from temporalio import workflow
from temporalio.exceptions import ActivityError

from .maintenance_schemas import (
    ProjectionCleanupResult,
    ReindexInput,
    ReindexResult,
    RelationRepairResult,
)
from .policies import DOCUMENT_RETRY, INDEX_QUEUE

_RELATION_REPAIR_PATCH = "harborrag-reindex-relation-repair"


@workflow.defn(name="harborrag.reindex")
class ReindexWorkflow:
    """Rebuild retrieval projections strictly from durable artifacts."""

    @workflow.run
    async def run(
        self,
        request: ReindexInput,
    ) -> ReindexResult:
        result = await workflow.execute_activity(
            "harborrag.reindex",
            request,
            task_queue=INDEX_QUEUE,
            start_to_close_timeout=timedelta(hours=6),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=DOCUMENT_RETRY,
            result_type=ReindexResult,
        )
        try:
            await workflow.execute_activity(
                "harborrag.cleanup_reindex_projections",
                request,
                task_queue=INDEX_QUEUE,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=DOCUMENT_RETRY,
                result_type=ProjectionCleanupResult,
            )
        except ActivityError:
            workflow.logger.warning("Projection cleanup remains queued after reindex")
        else:
            await self._repair_relations(request)
        return cast(ReindexResult, result)

    @staticmethod
    async def _repair_relations(request: ReindexInput) -> None:
        if not workflow.patched(_RELATION_REPAIR_PATCH):
            return
        await workflow.execute_activity(
            "harborrag.repair_reindex_relations",
            request,
            task_queue=INDEX_QUEUE,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=DOCUMENT_RETRY,
            result_type=RelationRepairResult,
        )
