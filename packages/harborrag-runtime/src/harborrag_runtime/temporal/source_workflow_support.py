from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

from temporalio import workflow
from temporalio.exceptions import ActivityError
from temporalio.workflow import ParentClosePolicy

from harborrag_runtime.temporal.failure_handling import durable_failure

from .maintenance_schemas import ProjectionCleanupResult
from .policies import temporal_retry_policy
from .schemas import (
    DocumentDispatchSummary,
    SourceBatchInput,
    SourceCancellationInput,
    SourceDiscoveryResult,
    SourceFailureInput,
    SourceIngestionInput,
    SourceIngestionResult,
)
from .source_workflow_timeouts import (
    CLEANUP_ACTIVITY_TIMEOUT,
    CONTROL_ACTIVITY_TIMEOUT,
    DISCOVERY_ACTIVITY_HEARTBEAT_TIMEOUT,
    DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT,
    DISCOVERY_ACTIVITY_TIMEOUT,
)


class SourceWorkflowSupportMixin:
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
            id=f"harborrag-source-batch:{request.task_id}:{self._batch_number}",
            task_queue=request.workflow_options.task_queues.transform,
            result_type=DocumentDispatchSummary,
            parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
        )
        batch_future = asyncio.ensure_future(handle)
        cancel_future = asyncio.create_task(workflow.wait_condition(lambda: self._cancel_requested))
        pause_relay = asyncio.create_task(self._relay_pause_signals(handle))
        try:
            done, _ = await workflow.wait(
                {batch_future, cancel_future},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if batch_future in done:
                cancel_future.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_future
                return cast(DocumentDispatchSummary, await batch_future), False
            await handle.signal("request_graceful_cancel")
            return cast(DocumentDispatchSummary, await batch_future), True
        finally:
            pause_relay.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pause_relay

    async def _relay_pause_signals(
        self,
        handle: workflow.ChildWorkflowHandle[Any, DocumentDispatchSummary],
    ) -> None:
        relayed = False
        while True:

            def _paused_differs_from(current: bool = relayed) -> bool:
                return self._paused != current

            await workflow.wait_condition(_paused_differs_from)
            relayed = self._paused
            await handle.signal("pause" if relayed else "resume")

    @staticmethod
    async def _record_hard_cancellation(request: SourceIngestionInput) -> None:
        cleanup = workflow.execute_activity(
            "harborrag.cancel_source_ingestion",
            SourceCancellationInput(task_id=request.task_id),
            task_queue=request.workflow_options.task_queues.discovery,
            start_to_close_timeout=CONTROL_ACTIVITY_TIMEOUT,
            schedule_to_start_timeout=DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT,
            retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
        )
        await asyncio.shield(cleanup)

    @staticmethod
    async def _discover(request: SourceIngestionInput) -> SourceDiscoveryResult:
        try:
            return cast(
                SourceDiscoveryResult,
                await workflow.execute_activity(
                    "harborrag.discover_source_items",
                    request,
                    task_queue=request.workflow_options.task_queues.discovery,
                    start_to_close_timeout=DISCOVERY_ACTIVITY_TIMEOUT,
                    heartbeat_timeout=DISCOVERY_ACTIVITY_HEARTBEAT_TIMEOUT,
                    schedule_to_start_timeout=DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT,
                    retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
                    result_type=SourceDiscoveryResult,
                ),
            )
        except ActivityError as error:
            error_code, _ = durable_failure(error)
            await workflow.execute_activity(
                "harborrag.record_source_failure",
                SourceFailureInput(task_id=request.task_id, error_code=error_code),
                task_queue=request.workflow_options.task_queues.discovery,
                start_to_close_timeout=CONTROL_ACTIVITY_TIMEOUT,
                schedule_to_start_timeout=DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT,
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
                start_to_close_timeout=CLEANUP_ACTIVITY_TIMEOUT,
                schedule_to_start_timeout=DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT,
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
            start_to_close_timeout=CONTROL_ACTIVITY_TIMEOUT,
            schedule_to_start_timeout=DISCOVERY_ACTIVITY_SCHEDULE_TO_START_TIMEOUT,
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
