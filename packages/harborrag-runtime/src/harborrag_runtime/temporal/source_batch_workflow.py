"""Bounded child-dispatch workflow for one source ingestion batch."""

from __future__ import annotations

import asyncio

from temporalio import workflow
from temporalio.workflow import ParentClosePolicy

from harborrag_core.ingestion import DocumentIngestionOutcome

from .schemas import DocumentDispatchSummary, DocumentIngestionInput, SourceBatchInput


@workflow.defn(name="harborrag.source_batch")
class SourceBatchWorkflow:
    """Dispatch document children in bounded waves that can stop gracefully."""

    def __init__(self) -> None:
        self._cancel_requested = False

    @workflow.run
    async def run(self, request: SourceBatchInput) -> DocumentDispatchSummary:
        summary = DocumentDispatchSummary()
        for start in range(
            request.start_index,
            request.end_index,
            request.document_concurrency,
        ):
            if self._cancel_requested:
                break
            end = min(request.end_index, start + request.document_concurrency)
            statuses = await asyncio.gather(
                *(
                    workflow.execute_child_workflow(
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
                    for index in range(start, end)
                )
            )
            for status in statuses:
                summary = summary.add(status)
        return summary

    @workflow.signal
    def request_graceful_cancel(self) -> None:
        """Stop dispatching after the currently active concurrency wave."""

        self._cancel_requested = True
