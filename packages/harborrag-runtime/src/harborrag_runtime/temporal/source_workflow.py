from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from datetime import timedelta
from typing import cast

from temporalio import workflow
from temporalio.exceptions import ActivityError, ChildWorkflowError
from temporalio.workflow import ParentClosePolicy

from harborrag_runtime.temporal.failure_handling import durable_failure

from .maintenance_schemas import ProjectionCleanupResult
from .policies import (
    temporal_retry_policy,
)
from .schemas import (
    DocumentDispatchSummary,
    SourceBatchInput,
    SourceCancellationInput,
    SourceContinuation,
    SourceDiscoveryResult,
    SourceFailureInput,
    SourceFinalizationInput,
    SourceIngestionInput,
    SourceIngestionResult,
    SourceIngestionStatus,
)


@workflow.defn(name="harborrag.source_ingestion")
class SourceIngestionWorkflow:
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
                start_to_close_timeout=timedelta(minutes=15),
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
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
            )
            raise
        self._status = result.status
        await self._cleanup_source(request)
        return cast(SourceIngestionResult, result)

    async def _initial_state(
        self, request: SourceIngestionInput
    ) -> tuple[SourceDiscoveryResult, int]:
        continuation = request.continuation
        if continuation is not None:
            self._summary = continuation.summary
            self._batch_number = continuation.batch_number
            return (
                SourceDiscoveryResult(
                    scan_id=continuation.scan_id,
                    plan_reference=continuation.plan_reference,
                    document_count=continuation.document_count,
                ),
                continuation.next_document_index,
            )
        try:
            return await self._discover(request), 0
        except asyncio.CancelledError:
            await self._record_hard_cancellation(request)
            raise

    async def _run_batch(
        self,
        request: SourceIngestionInput,
        discovery: SourceDiscoveryResult,
        start: int,
        end: int,
    ) -> tuple[DocumentDispatchSummary, bool]:
        handle = await workflow.start_child_workflow(
            "harborrag.source_batch",
            SourceBatchInput(
                task_id=request.task_id,
                tenant_id=request.tenant_id,
                connector_name=request.connector_name,
                plan_reference=discovery.plan_reference,
                start_index=start,
                end_index=end,
                batch_number=self._batch_number,
                document_concurrency=request.document_concurrency,
                workflow_options=request.workflow_options,
            ),
            id=(f"harborrag-source-batch:{request.task_id}:{self._batch_number}"),
            task_queue=request.workflow_options.task_queues.transform,
            result_type=DocumentDispatchSummary,
            parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
        )
        batch_future = asyncio.ensure_future(handle)
        cancel_future = asyncio.create_task(workflow.wait_condition(lambda: self._cancel_requested))
        done, _ = await workflow.wait(
            {batch_future, cancel_future}, return_when=asyncio.FIRST_COMPLETED
        )
        if batch_future in done:
            cancel_future.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_future
            return cast(DocumentDispatchSummary, await batch_future), False
        await handle.signal("request_graceful_cancel")
        return cast(DocumentDispatchSummary, await batch_future), True

    @staticmethod
    async def _record_hard_cancellation(request: SourceIngestionInput) -> None:
        cleanup = workflow.execute_activity(
            "harborrag.cancel_source_ingestion",
            SourceCancellationInput(task_id=request.task_id),
            task_queue=request.workflow_options.task_queues.discovery,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
        )
        await asyncio.shield(cleanup)

    @staticmethod
    async def _discover(
        request: SourceIngestionInput,
    ) -> SourceDiscoveryResult:
        try:
            return cast(
                SourceDiscoveryResult,
                await workflow.execute_activity(
                    "harborrag.discover_source_items",
                    request,
                    task_queue=request.workflow_options.task_queues.discovery,
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
                    result_type=SourceDiscoveryResult,
                ),
            )
        except ActivityError as error:
            error_code, _ = durable_failure(error)
            await workflow.execute_activity(
                "harborrag.record_source_failure",
                SourceFailureInput(
                    task_id=request.task_id,
                    error_code=error_code,
                ),
                task_queue=request.workflow_options.task_queues.discovery,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
            )
            raise

    @staticmethod
    async def _cleanup_source(request: SourceIngestionInput) -> None:
        try:
            await workflow.execute_activity(
                "harborrag.cleanup_source_projections",
                request,
                task_queue=request.workflow_options.task_queues.index,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=temporal_retry_policy(request.workflow_options.retries.document),
                result_type=ProjectionCleanupResult,
            )
        except ActivityError:
            workflow.logger.warning("Projection cleanup remains queued for source scope")

    async def _stop_requested(self) -> bool:
        if self._paused and not self._cancel_requested:
            self._status = "PAUSED"
            await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
            self._status = "CANCELLING" if self._cancel_requested else "RUNNING"
        return self._cancel_requested

    async def _cancelled_result(
        self,
        request: SourceIngestionInput,
        discovery: SourceDiscoveryResult,
    ) -> SourceIngestionResult:
        await workflow.execute_activity(
            "harborrag.cancel_source_ingestion",
            SourceCancellationInput(task_id=request.task_id),
            task_queue=request.workflow_options.task_queues.discovery,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
        )
        await self._cleanup_source(request)
        self._status = "CANCELLED"
        return SourceIngestionResult(
            task_id=request.task_id,
            scan_id=discovery.scan_id,
            discovered=discovery.document_count,
            published=self._summary.published,
            unchanged=self._summary.unchanged,
            failed=self._summary.failed,
            removal_candidates=(),
            unresolved_relations=0,
            status="CANCELLED",
        )

    @workflow.signal
    def pause(self) -> None:
        if not self._cancel_requested:
            self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

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
