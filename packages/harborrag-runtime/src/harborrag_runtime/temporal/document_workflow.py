from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.temporal.failure_handling import durable_failure

from .policies import (
    DISCOVERY_RETRY,
    DOCUMENT_RETRY,
    DOCUMENT_STAGES,
    INDEX_QUEUE,
    IO_QUEUE,
    PARSER_QUEUE,
)
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
        try:
            capture = await workflow.execute_activity(
                "harborrag.fetch_and_capture_raw",
                request,
                task_queue=IO_QUEUE,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=DOCUMENT_RETRY,
                result_type=RawCaptureResult,
            )
            failed_stage = "ParseAndNormalize"
            prepared = await workflow.execute_activity(
                "harborrag.parse_and_normalize",
                capture,
                task_queue=PARSER_QUEUE,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=DOCUMENT_RETRY,
                result_type=PreparedDocument,
            )
            if prepared.canonical_reference is not None:
                for stage_name, name, queue in DOCUMENT_STAGES:
                    failed_stage = stage_name
                    prepared = await workflow.execute_activity(
                        name,
                        prepared,
                        task_queue=queue,
                        start_to_close_timeout=timedelta(minutes=60),
                        heartbeat_timeout=timedelta(minutes=2),
                        retry_policy=DOCUMENT_RETRY,
                        result_type=PreparedDocument,
                    )
            failed_stage = "PublishVersion"
            return DocumentIngestionOutcome(
                await workflow.execute_activity(
                    "harborrag.publish_version",
                    prepared,
                    task_queue=INDEX_QUEUE,
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=DOCUMENT_RETRY,
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
                task_queue=IO_QUEUE,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=DISCOVERY_RETRY,
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
                task_queue=IO_QUEUE,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=DISCOVERY_RETRY,
            )
            return DocumentIngestionOutcome.FAILED
