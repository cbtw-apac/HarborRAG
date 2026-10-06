"""Bounded child-dispatch workflow for one source ingestion batch."""

from __future__ import annotations

import asyncio
import logging

from temporalio import workflow
from temporalio.exceptions import ChildWorkflowError
from temporalio.workflow import ParentClosePolicy

from harborrag_core.ingestion import DocumentIngestionOutcome

from .schemas import DocumentDispatchSummary, DocumentIngestionInput, SourceBatchInput

_SLIDING_WINDOW_PATCH = "harborrag-batch-sliding-window"
_CIRCUIT_BREAKER_PATCH = "harborrag-batch-circuit-breaker"
# Consecutive failed documents that pause the whole source. A healthy run fails
# documents sporadically; an unbroken run of failures means a shared dependency
# (object store, database, OCR) is down, and dispatching on only turns the rest
# of the backlog into failures -- 5,470 of them in one outage on 2026-10-01.
_CONSECUTIVE_FAILURE_PAUSE = 25
_logger = logging.getLogger("harborrag.runtime.temporal.source_batch")


def _sliding_window() -> bool:
    """Whether this batch dispatches through a sliding window rather than waves.

    Batches already running when the window shipped keep replaying the waves
    their history recorded. Outside a workflow there is no history to match.
    """

    return not workflow.in_workflow() or workflow.patched(_SLIDING_WINDOW_PATCH)


def _warn(message: str, *args: object) -> None:
    # workflow.logger is replay-aware (it stays quiet while replaying history)
    # but only exists inside a workflow; unit tests drive the class directly.
    (workflow.logger if workflow.in_workflow() else _logger).warning(message, *args)


def _circuit_breaker() -> bool:
    """Whether a run of failures may pause this batch (same replay rule as above)."""

    return not workflow.in_workflow() or workflow.patched(_CIRCUIT_BREAKER_PATCH)


@workflow.defn(name="harborrag.source_batch")
class SourceBatchWorkflow:
    """Dispatch document children through a bounded window that can stop gracefully."""

    def __init__(self) -> None:
        self._cancel_requested = False
        self._paused = False
        self._consecutive_failures = 0

    @workflow.run
    async def run(self, request: SourceBatchInput) -> DocumentDispatchSummary:
        if not _sliding_window():
            return await self._run_waves(request)
        return await self._run_window(request)

    async def _run_window(self, request: SourceBatchInput) -> DocumentDispatchSummary:
        """Keep ``document_concurrency`` documents in flight until the batch is done.

        Waves waited for their slowest document before starting any of the
        next, so a batch ran well below its concurrency whenever one document
        was slow; a window starts the next document as soon as any finishes.
        """

        summary = DocumentDispatchSummary()
        next_index = request.start_index
        in_flight: list[asyncio.Future[DocumentIngestionOutcome]] = []
        while True:
            while (
                next_index < request.end_index
                and len(in_flight) < request.document_concurrency
                and not self._paused
                and not self._cancel_requested
            ):
                in_flight.append(asyncio.ensure_future(self._document(request, next_index)))
                next_index += 1
            if not in_flight:
                if self._cancel_requested or next_index >= request.end_index:
                    return summary
                await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
                continue
            await workflow.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
            for future in [future for future in in_flight if future.done()]:
                outcome = future.result()
                summary = summary.add(outcome)
                await self._watch_failures(outcome)
            in_flight = [future for future in in_flight if not future.done()]

    async def _watch_failures(self, outcome: DocumentIngestionOutcome) -> None:
        """Pause the source after an unbroken run of failed documents.

        The pause goes to the parent source workflow, which reports PAUSED and
        relays the operator's resume back down. Batches whose history predates the
        breaker replay without it. A missing source item neither extends nor
        breaks the run: it is the item's fault, not a shared dependency's.
        """

        if outcome is DocumentIngestionOutcome.SOURCE_ITEM_MISSING:
            return
        if outcome != DocumentIngestionOutcome.FAILED:
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        if (
            self._consecutive_failures < _CONSECUTIVE_FAILURE_PAUSE
            or self._paused
            or self._cancel_requested
            or not _circuit_breaker()
        ):
            return
        _warn(
            "Pausing source after %d consecutive failed documents; "
            "resume the ingestion once the shared dependency has recovered",
            self._consecutive_failures,
        )
        self._paused = True
        self._consecutive_failures = 0
        parent = workflow.info().parent if workflow.in_workflow() else None
        if parent is not None:
            await workflow.get_external_workflow_handle(
                parent.workflow_id,
                run_id=parent.run_id,
            ).signal("pause")

    async def _run_waves(self, request: SourceBatchInput) -> DocumentDispatchSummary:
        summary = DocumentDispatchSummary()
        for start in range(
            request.start_index,
            request.end_index,
            request.document_concurrency,
        ):
            if self._paused:
                await workflow.wait_condition(lambda: not self._paused or self._cancel_requested)
            if self._cancel_requested:
                break
            end = min(request.end_index, start + request.document_concurrency)
            statuses = await asyncio.gather(
                *(self._document(request, index) for index in range(start, end))
            )
            for status in statuses:
                summary = summary.add(status)
        return summary

    @staticmethod
    async def _document(request: SourceBatchInput, index: int) -> DocumentIngestionOutcome:
        try:
            outcome: DocumentIngestionOutcome = await workflow.execute_child_workflow(
                "harborrag.document_ingestion",
                DocumentIngestionInput(
                    task_id=request.task_id,
                    tenant_id=request.tenant_id,
                    connector_name=request.connector_name,
                    plan_reference=request.plan_reference,
                    document_index=index,
                    workflow_options=request.workflow_options,
                ),
                id=f"harborrag-document:{request.task_id}:{request.batch_number}:{index}",
                task_queue=request.workflow_options.task_queues.transform,
                result_type=DocumentIngestionOutcome,
                parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
            )
            return outcome
        except ChildWorkflowError:
            # A document workflow only fails outright when even recording its
            # failure ran out of retries (a database outage, say). That is one
            # failed document, not a reason to fail the batch -- which used to
            # cancel every sibling in flight and fail the whole source task.
            _warn("Document workflow failed index=%d", index)
            return DocumentIngestionOutcome.FAILED

    @workflow.signal
    def request_graceful_cancel(self) -> None:
        """Start no further documents; the ones already in flight finish."""

        self._cancel_requested = True
        self._paused = False

    @workflow.signal
    def pause(self) -> None:
        """Start no further documents until resumed; the ones in flight finish."""

        if not self._cancel_requested:
            self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False
        self._consecutive_failures = 0
