from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.document_stage_catalog import DOCUMENT_STAGE_CATALOG
from harborrag_runtime.temporal.failure_handling import durable_failure

from .policies import temporal_retry_policy
from .schemas import (
    DocumentFailureInput,
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
)


@workflow.defn(name="harborrag.document_ingestion")
class DocumentIngestionWorkflow:
    """Run one independently retryable document through the release stages."""

    @workflow.run
    async def run(self, request: DocumentIngestionInput) -> DocumentIngestionOutcome:
        prepared: PreparedDocument | None = None
        failed_stage = "FetchAndCaptureRaw"
        queues = request.workflow_options.task_queues
        discovery_retry = temporal_retry_policy(request.workflow_options.retries.discovery)
        document_retry = temporal_retry_policy(request.workflow_options.retries.document)
        try:
            capture = await workflow.execute_activity(
                "harborrag.fetch_and_capture_raw",
                request,
                task_queue=queues.io,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=document_retry,
                result_type=RawCaptureResult,
            )
            failed_stage = "ParseAndNormalize"
            prepared = await workflow.execute_activity(
                "harborrag.parse_and_normalize",
                capture,
                task_queue=queues.parser,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=document_retry,
                result_type=PreparedDocument,
            )
            if prepared.canonical_reference is not None:
                for stage in DOCUMENT_STAGE_CATALOG:
                    failed_stage = stage.name
                    prepared = await workflow.execute_activity(
                        stage.activity,
                        prepared,
                        task_queue=queues.for_role(stage.task_queue_role),
                        start_to_close_timeout=timedelta(minutes=60),
                        heartbeat_timeout=timedelta(minutes=2),
                        retry_policy=document_retry,
                        result_type=PreparedDocument,
                    )
            failed_stage = "PublishVersion"
            return DocumentIngestionOutcome(
                await workflow.execute_activity(
                    "harborrag.publish_version",
                    prepared,
                    task_queue=queues.index,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=document_retry,
                    result_type=DocumentIngestionOutcome,
                )
            )
        except asyncio.CancelledError:
            cleanup = workflow.execute_activity(
                "harborrag.record_document_failure",
                DocumentFailureInput(
                    document=request,
                    prepared=prepared,
                    failed_stage=failed_stage,
                    error_type="cancelled",
                ),
                task_queue=queues.io,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=discovery_retry,
            )
            await asyncio.shield(cleanup)
            raise
        except ActivityError as error:
            error_type, _ = durable_failure(error)
            await workflow.execute_activity(
                "harborrag.record_document_failure",
                DocumentFailureInput(
                    document=request,
                    prepared=prepared,
                    failed_stage=failed_stage,
                    error_type=error_type,
                ),
                task_queue=queues.io,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=discovery_retry,
            )
            return DocumentIngestionOutcome.FAILED
