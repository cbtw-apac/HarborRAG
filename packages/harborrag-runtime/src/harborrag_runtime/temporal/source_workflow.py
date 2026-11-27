"""Temporal workflows that run a source ingestion."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import (
    ActivityError,
    ChildWorkflowError,
    FailureError,
    is_cancelled_exception,
)
from temporalio.workflow import ActivityCancellationType, ParentClosePolicy

from harborrag_core.ingestion.run_contracts import RunKind
from harborrag_engine.ingestion.source.tasks import source_scan_id
from harborrag_runtime.temporal.failure_handling import durable_failure

from .maintenance_schemas import ProjectionCleanupResult
from .policies import (
    temporal_retry_policy,
)
from .schemas import (
    DocumentDispatchSummary,
    ScheduledRunMetricInput,
    ScheduledSourceIngestionInput,
    ScheduledSourceIngestionResult,
    ScheduledSourcePreparation,
    ScheduledSourcePreparationInput,
    SourceBatchInput,
    SourceCancellationInput,
    SourceCancellationResult,
    SourceContinuation,
    SourceDiscoveryResult,
    SourceFailureInput,
    SourceFinalizationInput,
    SourceIngestionInput,
    SourceIngestionResult,
    SourceIngestionStatus,
    SourcePauseInput,
    SourceResumeInput,
    WorkflowExecutionControlInput,
)

_DISCOVER_FAILURE_STATUS_PATCH = "discover-failure-status"
_RUN_FAILURE_STATUS_PATCH = "run-failure-status"
_PAUSE_PERSISTENCE_PATCH = "harborrag-source-pause-persistence"
_PAUSE_RELAY_TEARDOWN_PATCH = "harborrag-source-pause-relay-teardown"
_SIGNAL_ONLY_BATCH_PAUSE_PATCH = "harborrag-source-signal-only-batch-pause"
_RECORDS_CANCELLATION_PATCH = "harborrag-source-records-cancellation"
_GUARDED_DISCOVERY_STOP_PATCH = "harborrag-source-guarded-discovery-stop"
_CANCEL_CLOSED_BATCH_PATCH = "harborrag-source-cancel-closed-batch"
_PROMPT_DISCOVERY_CANCEL_PATCH = "harborrag-source-prompt-discovery-cancel"


@workflow.defn(name="harborrag.scheduled_source_ingestion")
class ScheduledSourceIngestionWorkflow:
    """Workflow that prepares and runs one scheduled source ingestion."""

    @workflow.run
    async def run(
        self,
        request: ScheduledSourceIngestionInput,
    ) -> ScheduledSourceIngestionResult:
        """Run the scheduled ingestion.

        Backfills reuse a past firing's workflow ID; the run ID keeps each execution its own task
        and scan instead of replaying the old plan.

        Another ingestion holds the scope; its scan covers this firing. Warn: repeated skips mean a
        long-running or stranded scan.
        """
        info = workflow.info()
        scheduled_at = info.typed_search_attributes.get(
            SearchAttributeKey.for_datetime("TemporalScheduledStartTime")
        )
        schedule_to_start_seconds = (
            max(0.0, (info.workflow_start_time - scheduled_at).total_seconds())
            if scheduled_at is not None
            else None
        )
        workflow.logger.info(
            "Scheduled ingestion started schedule_id=%s workflow_id=%s run_id=%s",
            request.schedule_id,
            info.workflow_id,
            info.run_id,
        )
        await _record_scheduled_run_metric(
            request.schedule_id,
            "started",
            schedule_to_start_seconds=schedule_to_start_seconds,
        )
        task_id = _scheduled_task_id(request.schedule_id, info.workflow_id, info.run_id)
        try:
            preparation = await workflow.execute_activity(
                "harborrag.prepare_scheduled_source",
                ScheduledSourcePreparationInput(
                    task_id=task_id,
                    tenant_id=request.tenant_id,
                    connection_id=request.connection_id,
                    source_scope_id=request.source_scope_id,
                    force_reprocess=request.force_reprocess,
                ),
                result_type=ScheduledSourcePreparation,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            if preparation.source is None:
                workflow.logger.warning(
                    "Scheduled ingestion skipped; source scope busy schedule_id=%s "
                    "workflow_id=%s source_scope_id=%s",
                    request.schedule_id,
                    info.workflow_id,
                    preparation.source_scope_id,
                )
                await _record_scheduled_run_metric(request.schedule_id, "skipped")
                return ScheduledSourceIngestionResult(
                    schedule_id=request.schedule_id, outcome="skipped"
                )
            handle = await workflow.start_child_workflow(
                "harborrag.source_ingestion",
                preparation.source,
                id=RunKind.SOURCE.workflow_id(task_id),
                task_queue=preparation.source.workflow_options.task_queues.discovery,
                result_type=SourceIngestionResult,
                parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
            )
            workflow.logger.info(
                "Scheduled ingestion child started schedule_id=%s workflow_id=%s "
                "run_id=%s child_workflow_id=%s",
                request.schedule_id,
                info.workflow_id,
                info.run_id,
                handle.id,
            )
            result = cast(SourceIngestionResult, await handle)
        except (Exception, asyncio.CancelledError) as error:
            outcome = "cancelled" if is_cancelled_exception(error) else "failed"
            workflow.logger.error(
                "Scheduled ingestion %s schedule_id=%s workflow_id=%s run_id=%s error_type=%s",
                outcome,
                request.schedule_id,
                info.workflow_id,
                info.run_id,
                type(error).__name__,
            )
            await _record_scheduled_run_metric(request.schedule_id, outcome)
            raise
        outcome = _RESULT_OUTCOMES.get(result.status, "failed")
        workflow.logger.info(
            "Scheduled ingestion finished outcome=%s schedule_id=%s workflow_id=%s run_id=%s",
            outcome,
            request.schedule_id,
            info.workflow_id,
            info.run_id,
        )
        await _record_scheduled_run_metric(request.schedule_id, outcome)
        return ScheduledSourceIngestionResult(
            schedule_id=request.schedule_id,
            outcome=outcome,
            task_id=task_id,
            result=result,
        )


_RESULT_OUTCOMES = {
    "COMPLETED": "completed",
    "PARTIAL": "partial",
    "CANCELLED": "cancelled",
    "FAILED": "failed",
}
"""Termination and execution timeouts end the run without running workflow code, so they are never
recorded here.
"""


async def _record_scheduled_run_metric(
    schedule_id: str,
    outcome: str,
    *,
    schedule_to_start_seconds: float | None = None,
) -> None:
    try:
        await workflow.execute_activity(
            "harborrag.record_scheduled_run_metrics",
            ScheduledRunMetricInput(
                schedule_id=schedule_id,
                outcome=outcome,
                schedule_to_start_seconds=schedule_to_start_seconds,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
    except Exception as error:
        workflow.logger.warning(
            "Scheduled run metric recording failed schedule_id=%s outcome=%s error_type=%s",
            schedule_id,
            outcome,
            type(error).__name__,
        )


def _scheduled_task_id(schedule_id: str, workflow_id: str, run_id: str) -> str:
    digest = hashlib.sha256(f"{schedule_id}:{workflow_id}:{run_id}".encode()).hexdigest()[:32]
    return f"scheduled-{digest}"


def _prompt_discovery_cancel() -> bool:
    """Whether a graceful cancel stops discovery instead of waiting for it to end.

    Runs recorded before the patch wait for it. Outside a workflow they wait too, since
    cancelling the activity needs the workflow event loop.
    """
    return workflow.in_workflow() and workflow.patched(_PROMPT_DISCOVERY_CANCEL_PATCH)


def _current_details(status: str, task_id: str) -> str:
    """Describe a paused or cancelling run for the Temporal UI, with IDs and states only."""
    since = workflow.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    if status == "PAUSED":
        return (
            f"Paused by HarborRAG at {since}; "
            f"resume with POST /v1/admin/ingestions/{task_id}/resume"
        )
    if status == "CANCELLING":
        return f"Cancelling at HarborRAG's request since {since}"
    return ""


@workflow.defn(name="harborrag.source_ingestion")
class SourceIngestionWorkflow:
    """Workflow that discovers a source's documents and ingests them in batches."""

    def __init__(self) -> None:
        self._summary = DocumentDispatchSummary()
        self._discovered = 0
        self._skipped_by_reason: dict[str, int] = {}
        self._batch_number = 0
        self._task_id = "pending"
        self._status = "PENDING"
        self._paused = False
        self._pause_persisted = False
        self._pause_relay_applied = False
        self._relay_busy = False
        self._relay_stopping = False
        self._cancel_requested = False

    @workflow.run
    async def run(
        self,
        request: SourceIngestionInput,
    ) -> SourceIngestionResult:
        """Run the source ingestion.

        A pause that arrives during discovery is handled once discovery ends. A graceful
        cancel stops discovery and ends the run cancelled. In runs recorded before that
        patch, the cancel also waits for discovery to end. Either is handled inside the
        run's failure and cancellation recording.
        """
        self._task_id = request.task_id
        self._set_status("RUNNING")
        discovery, start_index = await self._initial_state(request)
        self._count_discovery(discovery)
        guarded_stop = not workflow.in_workflow() or workflow.patched(_GUARDED_DISCOVERY_STOP_PATCH)
        if not guarded_stop and await self._stop_requested(request):
            return await self._cancelled_result(request, discovery)
        completed_in_run = 0
        try:
            if discovery is None or (guarded_stop and await self._stop_requested(request)):
                return await self._cancelled_result(request, discovery)
            for start in range(
                start_index,
                discovery.document_count,
                request.batch_size,
            ):
                if await self._stop_requested(request):
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
                if await self._stop_requested(request):
                    return await self._cancelled_result(request, discovery)
                self._continue_as_new_if_due(request, discovery, end, completed_in_run)
            if await self._stop_requested(request):
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
                start_to_close_timeout=timedelta(
                    seconds=request.workflow_options.timeouts.finalization_seconds
                ),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
                result_type=SourceIngestionResult,
            )
        except asyncio.CancelledError:
            await self._record_hard_cancellation(request)
            raise
        except (ActivityError, ChildWorkflowError) as error:
            await self._record_failed_step(request, error)
            raise
        self._set_status(result.status)
        await self._cleanup_source(request)
        return cast(SourceIngestionResult, result)

    def _set_status(self, status: str) -> None:
        """Set the run's status and the details the Temporal UI shows for it.

        A paused run is paused by signal, so Temporal shows it as running. The details say it
        is paused, since when, and how to resume it. A status set again keeps its details.
        """
        if status == self._status:
            return
        self._status = status
        if workflow.in_workflow():
            workflow.set_current_details(_current_details(status, self._task_id))

    def _count_discovery(self, discovery: SourceDiscoveryResult | None) -> None:
        """Report the documents and skips of a discovery that finished."""
        if discovery is not None:
            self._discovered = discovery.document_count
            self._skipped_by_reason = dict(discovery.skipped_by_reason)

    async def _record_failed_step(
        self,
        request: SourceIngestionInput,
        error: ActivityError | ChildWorkflowError,
    ) -> None:
        """Record a failed batch or finalization as the run's cancellation or failure."""
        if is_cancelled_exception(error) and workflow.patched(_RECORDS_CANCELLATION_PATCH):
            await self._record_hard_cancellation(request)
            return
        await self._record_run_failure(request, error)

    def _continue_as_new_if_due(
        self,
        request: SourceIngestionInput,
        discovery: SourceDiscoveryResult,
        end: int,
        completed_in_run: int,
    ) -> None:
        """Continue as a new run from ``end`` once this run's batch budget is spent.

        The budget is spent after ``continue_after_batches`` batches, or earlier when
        Temporal suggests it. A run whose last batch ended at ``end`` goes on to
        finalize instead.
        """
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
                        skipped_by_reason=self._skipped_by_reason,
                    ),
                )
            )

    async def _record_run_failure(
        self,
        request: SourceIngestionInput,
        error: ActivityError | ChildWorkflowError,
    ) -> None:
        """Persist a hard batch or finalization failure before it closes the run.

        A child batch, or finalization itself, can exhaust its retries and fail
        hard. Without this, the failure propagated straight out of the workflow
        with no `record_source_failure` call, so the control-plane task row stayed
        RUNNING forever even though Temporal itself considered the run failed.

        Like discovery: the closed workflow's get_status must say FAILED.
        """
        if workflow.patched(_RUN_FAILURE_STATUS_PATCH):
            self._set_status("FAILED")
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

    async def _initial_state(
        self, request: SourceIngestionInput
    ) -> tuple[SourceDiscoveryResult | None, int]:
        """Return the run's discovery, or None when a graceful cancel stopped it, and its start."""
        continuation = request.continuation
        if continuation is not None:
            self._summary = continuation.summary
            self._batch_number = continuation.batch_number
            return (
                SourceDiscoveryResult(
                    scan_id=continuation.scan_id,
                    plan_reference=continuation.plan_reference,
                    document_count=continuation.document_count,
                    skipped_by_reason=continuation.skipped_by_reason,
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
        self._relay_busy = False
        self._relay_stopping = False
        pause_relay = asyncio.create_task(self._relay_pause_signals(handle, request))
        try:
            done, _ = await workflow.wait(
                {batch_future, cancel_future}, return_when=asyncio.FIRST_COMPLETED
            )
            if batch_future in done:
                cancel_future.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_future
                return cast(DocumentDispatchSummary, await batch_future), False
            await self._cancel_batch(handle)
            return cast(DocumentDispatchSummary, await batch_future), True
        finally:
            await self._stop_pause_relay(pause_relay)

    @staticmethod
    async def _cancel_batch(
        handle: workflow.ChildWorkflowHandle[Any, DocumentDispatchSummary],
    ) -> None:
        """Ask the batch child to stop gracefully.

        A batch that closed before this workflow saw it finish rejects the signal.
        Its result still stands, and the run is cancelled with it.
        """
        try:
            await handle.signal("request_graceful_cancel")
        except FailureError as error:
            if is_cancelled_exception(error) or not workflow.patched(_CANCEL_CLOSED_BATCH_PATCH):
                raise
            workflow.logger.info(
                "Graceful cancel skipped for closed batch workflow_id=%s", handle.id
            )

    async def _stop_pause_relay(self, pause_relay: asyncio.Task[None]) -> None:
        """End the batch's pause relay without failing the run.

        Cancelling a relay mid-step resolved its activity or signal as cancelled,
        which escaped as an `ActivityError` and recorded a successful batch FAILED;
        the cancelled activity also kept running and could commit after a later
        resume. A busy relay now finishes its step, skipping the closed child, and
        returns. An idle one has nothing in flight and is still cancelled.
        """
        if self._relay_busy and workflow.patched(_PAUSE_RELAY_TEARDOWN_PATCH):
            self._relay_stopping = True
        else:
            pause_relay.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pause_relay

    async def _relay_pause_signals(
        self,
        handle: workflow.ChildWorkflowHandle[Any, DocumentDispatchSummary],
        request: SourceIngestionInput,
    ) -> None:
        """Forward this workflow's pause/resume signals to the active batch child.

        Without this, a pause requested while a batch's document waves are
        dispatching would sit unenforced until that batch drained on its own,
        since the child has no way to observe the parent's `_paused` state.

        Woken only to stop: the batch closed with nothing left to relay.
        """
        relayed = False
        while True:

            def _relay_wakes(current: bool = relayed) -> bool:
                return self._paused != current or self._relay_stopping

            await workflow.wait_condition(_relay_wakes)
            if self._paused == relayed:
                return
            relayed = self._paused
            self._relay_busy = True
            await self._relay_step(handle, request, paused=relayed)
            self._relay_busy = False

    async def _relay_step(
        self,
        handle: workflow.ChildWorkflowHandle[Any, DocumentDispatchSummary],
        request: SourceIngestionInput,
        *,
        paused: bool,
    ) -> None:
        """Persisted even while stopping, so the task row matches `_paused`.

        A stopped relay leaves this to the batch boundary's own pause.
        """
        if not workflow.patched(_PAUSE_PERSISTENCE_PATCH):
            await self._signal_batch(handle, "pause" if paused else "resume")
            return
        await workflow.execute_activity(
            "harborrag.pause_source_ingestion" if paused else "harborrag.resume_source_ingestion",
            SourcePauseInput(task_id=self._task_id)
            if paused
            else SourceResumeInput(task_id=self._task_id),
            task_queue=request.workflow_options.task_queues.discovery,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
        )
        if paused:
            await self._signal_batch(handle, "pause")
            # TODO: pause natively again on Temporal server >= 1.32 with temporalio >= 1.35.
            if not workflow.patched(_SIGNAL_ONLY_BATCH_PAUSE_PATCH):
                await self._control_batch(handle, request, "harborrag.pause_workflow_execution")
            if not self._relay_stopping:
                self._pause_relay_applied = True
        else:
            await self._control_batch(handle, request, "harborrag.unpause_workflow_execution")
            await self._signal_batch(handle, "resume")
            self._pause_relay_applied = False

    async def _signal_batch(
        self,
        handle: workflow.ChildWorkflowHandle[Any, DocumentDispatchSummary],
        name: str,
    ) -> None:
        """Send a signal to the batch child workflow.

        Only a closed child rejects a signal: it closed before this workflow saw it finish, and
        there is nothing left to pause or resume.
        """
        if self._relay_stopping:
            return
        try:
            await handle.signal(name)
        except FailureError as error:
            if is_cancelled_exception(error) or not workflow.patched(_PAUSE_RELAY_TEARDOWN_PATCH):
                raise
            workflow.logger.info(
                "Pause relay skipped signal=%s for closed batch workflow_id=%s",
                name,
                handle.id,
            )

    async def _control_batch(
        self,
        handle: workflow.ChildWorkflowHandle[Any, DocumentDispatchSummary],
        request: SourceIngestionInput,
        activity_name: str,
    ) -> None:
        if self._relay_stopping:
            return
        await workflow.execute_activity(
            activity_name,
            WorkflowExecutionControlInput(workflow_id=handle.id),
            task_queue=request.workflow_options.task_queues.discovery,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
        )

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

    async def _discover(
        self,
        request: SourceIngestionInput,
    ) -> SourceDiscoveryResult | None:
        """Run source discovery as an activity.

        Each retry resumes from the last persisted page, so the retry bound covers one attempt, not
        the discovery.

        Returns:
            The discovery, or None when a graceful cancel stopped it.
        """
        try:
            if _prompt_discovery_cancel():
                return await self._discover_until_cancelled(request)
            return cast(
                SourceDiscoveryResult,
                await workflow.execute_activity(
                    "harborrag.discover_source_items",
                    request,
                    task_queue=request.workflow_options.task_queues.discovery,
                    start_to_close_timeout=timedelta(
                        seconds=request.workflow_options.timeouts.discovery_seconds
                    ),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
                    result_type=SourceDiscoveryResult,
                ),
            )
        except ActivityError as error:
            if is_cancelled_exception(error) and workflow.patched(_RECORDS_CANCELLATION_PATCH):
                await self._record_hard_cancellation(request)
                raise
            if workflow.patched(_DISCOVER_FAILURE_STATUS_PATCH):
                self._set_status("FAILED")
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

    async def _discover_until_cancelled(
        self,
        request: SourceIngestionInput,
    ) -> SourceDiscoveryResult | None:
        """Run discovery, and cancel it when a graceful cancel is requested first.

        The cancel reaches the discovery attempt at its next heartbeat. The workflow waits for
        the attempt to stop, so no walk of the source outlives the run.

        Returns:
            The discovery, or None when the cancel stopped it.
        """
        discovery = workflow.start_activity(
            "harborrag.discover_source_items",
            request,
            task_queue=request.workflow_options.task_queues.discovery,
            start_to_close_timeout=timedelta(
                seconds=request.workflow_options.timeouts.discovery_seconds
            ),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
            result_type=SourceDiscoveryResult,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        await self._cancel_on_request(discovery)
        try:
            return cast(SourceDiscoveryResult, discovery.result())
        except ActivityError as error:
            if not (self._cancel_requested and is_cancelled_exception(error)):
                raise
        return None

    async def _cancel_on_request(self, discovery: workflow.ActivityHandle[Any]) -> None:
        """Wait for discovery to end, cancelling it once a graceful cancel is requested.

        A Temporal cancel of the run also cancels discovery, without waiting for it.
        """
        cancel = asyncio.ensure_future(workflow.wait_condition(lambda: self._cancel_requested))
        waiting: list[asyncio.Future[Any]] = [discovery, cancel]
        try:
            await workflow.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            discovery.cancel()
            raise
        finally:
            if not cancel.done():
                cancel.cancel()
        if not discovery.done():
            discovery.cancel()
            await workflow.wait([discovery])

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

    async def _stop_requested(self, request: SourceIngestionInput) -> bool:
        if self._paused and not self._cancel_requested:
            self._set_status("PAUSED")
            persist_pause = workflow.patched(_PAUSE_PERSISTENCE_PATCH)
            if persist_pause and not self._pause_persisted:
                await workflow.execute_activity(
                    "harborrag.pause_source_ingestion",
                    SourcePauseInput(task_id=self._task_id),
                    task_queue=request.workflow_options.task_queues.discovery,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
                )
                self._pause_persisted = True
                self._pause_relay_applied = True
            await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
            self._pause_relay_applied = False
            if persist_pause and not self._cancel_requested:
                await workflow.execute_activity(
                    "harborrag.resume_source_ingestion",
                    SourceResumeInput(task_id=self._task_id),
                    task_queue=request.workflow_options.task_queues.discovery,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
                )
                self._pause_persisted = False
            self._set_status("CANCELLING" if self._cancel_requested else "RUNNING")
        return self._cancel_requested

    async def _cancelled_result(
        self,
        request: SourceIngestionInput,
        discovery: SourceDiscoveryResult | None,
    ) -> SourceIngestionResult:
        """Record the graceful cancel and return the run's counts, with every skip so far.

        A cancel activity from before it returned skips, or its result in an older
        history, leaves the discovery skips in place. A run cancelled during discovery
        has no plan, so it reports no documents, under the scan its discovery opened.
        """
        cancelled: SourceCancellationResult | None = await workflow.execute_activity(
            "harborrag.cancel_source_ingestion",
            SourceCancellationInput(task_id=request.task_id),
            task_queue=request.workflow_options.task_queues.discovery,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
            result_type=SourceCancellationResult,
        )
        if cancelled is not None:
            self._skipped_by_reason = dict(cancelled.skipped_by_reason)
        await self._cleanup_source(request)
        self._set_status("CANCELLED")
        return SourceIngestionResult(
            task_id=request.task_id,
            scan_id=source_scan_id(request.task_id) if discovery is None else discovery.scan_id,
            discovered=self._discovered,
            published=self._summary.published,
            unchanged=self._summary.unchanged,
            failed=self._summary.failed,
            removal_candidates=(),
            unresolved_relations=0,
            status="CANCELLED",
            skipped=sum(self._skipped_by_reason.values()),
            skipped_by_reason=dict(self._skipped_by_reason),
        )

    @workflow.signal
    def pause(self) -> None:
        """Pause dispatch of further documents."""
        if not self._cancel_requested and not self._paused:
            self._paused = True
            self._pause_persisted = False
            self._pause_relay_applied = False
            self._set_status("PAUSED")

    @workflow.signal
    def resume(self) -> None:
        """Resume dispatch of documents."""
        was_paused = self._paused
        self._paused = False
        if was_paused and not self._cancel_requested:
            self._pause_persisted = False
            self._pause_relay_applied = False
            self._set_status("RUNNING")

    @workflow.signal
    def request_graceful_cancel(self) -> None:
        """Stop dispatching, and stop a discovery in progress."""
        self._cancel_requested = True
        self._paused = False
        self._set_status("CANCELLING")

    @workflow.query
    def get_status(self) -> SourceIngestionStatus:
        """Return the workflow's status."""
        return SourceIngestionStatus(
            task_id=self._task_id,
            status=self._status,
            paused=self._paused,
            cancel_requested=self._cancel_requested,
            pause_applied=self._pause_relay_applied,
        )

    @workflow.query
    def get_progress(self) -> dict[str, int]:
        """Return document counts so far."""
        return {
            "discovered": self._discovered,
            "published": self._summary.published,
            "unchanged": self._summary.unchanged,
            "failed": self._summary.failed,
            "skipped": sum(self._skipped_by_reason.values()),
            "completed_batches": self._batch_number,
        }
