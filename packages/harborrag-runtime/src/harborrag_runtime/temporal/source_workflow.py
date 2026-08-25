from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import cast

from temporalio import workflow
from temporalio.exceptions import ActivityError, ChildWorkflowError
from temporalio.workflow import ParentClosePolicy

from harborrag_runtime.temporal.failure_handling import durable_failure

from .maintenance_schemas import ProjectionCleanupResult
from .policies import (
    temporal_retry_policy,
)
from .source_workflow_timeouts import (
    CONTROL_ACTIVITY_TIMEOUT,
    DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT,
    FINALIZE_ACTIVITY_TIMEOUT,
)
from .source_workflow_support import SourceWorkflowSupportMixin
from .schemas import (
    DocumentDispatchSummary,
    SourceContinuation,
    SourceFailureInput,
    SourceFinalizationInput,
    SourceIngestionInput,
    SourceIngestionResult,
    SourceIngestionStatus,
)


@workflow.defn(name="harborrag.source_ingestion")
class SourceIngestionWorkflow(SourceWorkflowSupportMixin):
    def __init__(self) -> None:
        self._summary = DocumentDispatchSummary()
        self._discovered = 0
        self._batch_number = 0
        self._task_id = "pending"
        self._status = "PENDING"
        self._paused = False
        self._cancel_requested = False

    @workflow.run
    async def run(
        self,
        request: SourceIngestionInput,
    ) -> SourceIngestionResult:
        self._task_id = request.task_id
        self._status = "RUNNING"
        discovery, start_index = await self._initial_state(request)
        self._discovered = discovery.document_count
        if await self._stop_requested():
            return await self._cancelled_result(request, discovery)
        completed_in_run = 0
        try:
            for start in range(
                start_index,
                discovery.document_count,
                request.batch_size,
            ):
                if await self._stop_requested():
                    return await self._cancelled_result(request, discovery)
                end = min(discovery.document_count, start + request.batch_size)
                batch_result, cancelled_during_batch = await self._run_batch(
                    request,
                    discovery,
                    start,
                    end,
                )
                self._summary = self._summary.merge(batch_result)
                if cancelled_during_batch:
                    return await self._cancelled_result(request, discovery)
                self._batch_number += 1
                completed_in_run += 1
                if await self._stop_requested():
                    return await self._cancelled_result(request, discovery)
                if end < discovery.document_count and (
                    completed_in_run >= request.continue_after_batches
                    or workflow.info().is_continue_as_new_suggested()
                ):
                    workflow.continue_as_new(
                        replace(
                            request,
                            continuation=SourceContinuation(
                                scan_id=discovery.scan_id,
                                plan_reference=discovery.plan_reference,
                                document_count=discovery.document_count,
                                next_document_index=end,
                                batch_number=self._batch_number,
                                summary=self._summary,
                            ),
                        )
                    )
            if await self._stop_requested():
                return await self._cancelled_result(request, discovery)
            result = await workflow.execute_activity(
                "harborrag.finalize_source_ingestion",
                SourceFinalizationInput(
                    source=request,
                    scan_id=discovery.scan_id,
                    plan_reference=discovery.plan_reference,
                    summary=self._summary,
                ),
                task_queue=request.workflow_options.task_queues.discovery,
                start_to_close_timeout=FINALIZE_ACTIVITY_TIMEOUT,
                schedule_to_start_timeout=DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT,
                retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
                result_type=SourceIngestionResult,
            )
        except asyncio.CancelledError:
            await self._record_hard_cancellation(request)
            raise
        except (ActivityError, ChildWorkflowError) as error:
            # A child batch, or finalization itself, can exhaust its retries
            # and fail hard. Without this, the failure propagated straight
            # out of the workflow with no `record_source_failure` call, so
            # the control-plane task row stayed RUNNING forever even though
            # Temporal itself considered the run failed.
            error_code, _ = durable_failure(error)
            await workflow.execute_activity(
                "harborrag.record_source_failure",
                SourceFailureInput(
                    task_id=request.task_id,
                    error_code=error_code,
                ),
                task_queue=request.workflow_options.task_queues.discovery,
                start_to_close_timeout=CONTROL_ACTIVITY_TIMEOUT,
                schedule_to_start_timeout=DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT,
                retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
            )
            raise
        self._status = result.status
        await self._cleanup_source(request)
        return cast(SourceIngestionResult, result)

    @workflow.signal
    def pause(self) -> None:
        if not self._cancel_requested:
            self._paused = True
            self._status = "PAUSED"

    @workflow.signal
    def resume(self) -> None:
        was_paused = self._paused
        self._paused = False
        if was_paused and not self._cancel_requested:
            self._status = "RUNNING"

    @workflow.signal
    def request_graceful_cancel(self) -> None:
        self._cancel_requested = True
        self._paused = False
        self._status = "CANCELLING"

    @workflow.query
    def get_status(self) -> SourceIngestionStatus:
        return SourceIngestionStatus(
            task_id=self._task_id,
            status=self._status,
            paused=self._paused,
            cancel_requested=self._cancel_requested,
        )

    @workflow.query
    def get_progress(self) -> dict[str, int]:
        return {
            "discovered": self._discovered,
            "published": self._summary.published,
            "unchanged": self._summary.unchanged,
            "failed": self._summary.failed,
            "completed_batches": self._batch_number,
        }
