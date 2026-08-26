"""Durable, artifact-first retries for failed ingestion documents."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import cast

from temporalio import workflow
from temporalio.exceptions import ActivityError, ChildWorkflowError
from temporalio.workflow import ParentClosePolicy

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.temporal.failure_handling import durable_failure

from .policies import temporal_retry_policy
from .schemas import (
    DocumentDispatchSummary,
    RetryDocumentFailureInput,
    RetryDocumentInput,
    RetryFailuresInput,
    RetryFailuresResult,
    RetryFinalizationInput,
    RetryPreparationResult,
    RetryTaskFailureInput,
)


@workflow.defn(name="harborrag.document_retry")
class DocumentRetryWorkflow:
    @workflow.run
    async def run(self, request: RetryDocumentInput) -> DocumentIngestionOutcome:
        queues = request.workflow_options.task_queues
        retries = request.workflow_options.retries
        try:
            return DocumentIngestionOutcome(
                await workflow.execute_activity(
                    "harborrag.retry_document_release",
                    request,
                    task_queue=queues.io,
                    start_to_close_timeout=timedelta(minutes=60),
                    retry_policy=temporal_retry_policy(retries.document),
                    result_type=DocumentIngestionOutcome,
                )
            )
        except ActivityError as error:
            error_type, _ = durable_failure(error)
            await workflow.execute_activity(
                "harborrag.record_retry_document_failure",
                RetryDocumentFailureInput(
                    document=request,
                    error_type=error_type,
                ),
                task_queue=queues.io,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=temporal_retry_policy(retries.discovery),
            )
            return DocumentIngestionOutcome.FAILED


@workflow.defn(name="harborrag.retry_failures")
class RetryFailuresWorkflow:
    @workflow.run
    async def run(self, request: RetryFailuresInput) -> RetryFailuresResult:
        queues = request.workflow_options.task_queues
        retries = request.workflow_options.retries
        try:
            prepared = cast(
                RetryPreparationResult,
                await workflow.execute_activity(
                    "harborrag.prepare_retry_failures",
                    request,
                    task_queue=queues.discovery,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=temporal_retry_policy(retries.discovery),
                    result_type=RetryPreparationResult,
                ),
            )
            summary = DocumentDispatchSummary()
            for start in range(0, prepared.document_count, request.document_concurrency):
                end = min(prepared.document_count, start + request.document_concurrency)
                statuses = await asyncio.gather(
                    *(
                        workflow.execute_child_workflow(
                            "harborrag.document_retry",
                            RetryDocumentInput(
                                retry_task_id=request.retry_task_id,
                                original_task_id=request.original_task_id,
                                tenant_id=request.tenant_id,
                                plan_reference=prepared.plan_reference,
                                document_index=index,
                                workflow_options=request.workflow_options,
                            ),
                            id=f"harborrag-document-retry:{request.retry_task_id}:{index}",
                            task_queue=queues.transform,
                            result_type=DocumentIngestionOutcome,
                            parent_close_policy=ParentClosePolicy.REQUEST_CANCEL,
                        )
                        for index in range(start, end)
                    )
                )
                for document_status in statuses:
                    summary = summary.add(document_status)
            await workflow.execute_activity(
                "harborrag.finalize_retry_failures",
                RetryFinalizationInput(
                    retry_task_id=request.retry_task_id,
                    selected=prepared.document_count,
                    summary=summary,
                ),
                task_queue=queues.discovery,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=temporal_retry_policy(retries.discovery),
            )
        except (ActivityError, ChildWorkflowError) as error:
            # Preparation, a child document-retry, or finalization itself can
            # exhaust its retries and fail hard. Without this, the failure
            # propagated straight out of the workflow with no failure record,
            # leaving the control-plane retry-task row stuck non-terminal even
            # though Temporal itself considered the run failed.
            error_code, _ = durable_failure(error)
            await workflow.execute_activity(
                "harborrag.record_retry_failures_task_failure",
                RetryTaskFailureInput(
                    retry_task_id=request.retry_task_id,
                    error_code=error_code,
                ),
                task_queue=queues.discovery,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=temporal_retry_policy(retries.discovery),
            )
            raise
        completed = summary.published + summary.unchanged
        if summary.failed and completed:
            status = "PARTIAL"
        elif summary.failed:
            status = "FAILED"
        else:
            status = "COMPLETED"
        return RetryFailuresResult(
            retry_task_id=request.retry_task_id,
            selected=prepared.document_count,
            published=summary.published,
            unchanged=summary.unchanged,
            failed=summary.failed,
            status=status,
        )
