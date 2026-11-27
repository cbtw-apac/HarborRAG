"""Temporal workflow that rebuilds retrieval projections from durable artifacts.

Workflow code stays deterministic: a change to the activities a run schedules goes behind
a ``workflow.patched`` marker, so recorded histories still replay.
"""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from harborrag_core.observability.log_events import FailureKind, log_failure

from .maintenance_schemas import (
    ProjectionCleanupResult,
    ReindexInput,
    ReindexResult,
    RelationRepairResult,
)
from .policies import temporal_retry_policy

_RELATION_REPAIR_PATCH = "harborrag-reindex-relation-repair"


@workflow.defn(name="harborrag.reindex")
class ReindexWorkflow:
    """Rebuild retrieval projections strictly from durable artifacts."""

    @workflow.run
    async def run(
        self,
        request: ReindexInput,
    ) -> ReindexResult:
        """Reindex, then clean the projections it left pending and repair relations.

        A cleanup that fails stays queued for a later run and does not fail the reindex,
        but relations are repaired only after a cleanup that succeeded.

        Args:
            request: The reindex job and its workflow options.

        Returns:
            The reindex activity's result.
        """
        queue = request.workflow_options.task_queues.index
        retry = temporal_retry_policy(request.workflow_options.retries.document)
        result = await workflow.execute_activity(
            "harborrag.reindex",
            request,
            task_queue=queue,
            start_to_close_timeout=timedelta(hours=6),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=retry,
            result_type=ReindexResult,
        )
        try:
            await workflow.execute_activity(
                "harborrag.cleanup_reindex_projections",
                request,
                task_queue=queue,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=retry,
                result_type=ProjectionCleanupResult,
            )
        except ActivityError as error:
            log_failure(
                workflow.logger,
                "temporal.reindex_cleanup_deferred",
                error,
                FailureKind.EXPECTED,
                reindex_job_id=request.reindex_job_id,
            )
        else:
            await self._repair_relations(request, queue, retry)
        return cast(ReindexResult, result)

    @staticmethod
    async def _repair_relations(
        request: ReindexInput,
        queue: str,
        retry: RetryPolicy,
    ) -> None:
        """Restore source edges after the cleanup, in runs that reached the patch."""
        if not workflow.patched(_RELATION_REPAIR_PATCH):
            return
        await workflow.execute_activity(
            "harborrag.repair_reindex_relations",
            request,
            task_queue=queue,
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=retry,
            result_type=RelationRepairResult,
        )
