"""Temporal workflow that runs one document from capture to publishing.

Workflow code stays deterministic. Every read, write and model call happens in an
activity, and a changed activity option must keep in-flight runs replaying.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, is_cancelled_exception

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_runtime.document_stage_catalog import DOCUMENT_STAGE_CATALOG
from harborrag_runtime.temporal.failure_handling import durable_failure, durable_skip_reason
from harborrag_runtime.temporal_models import ActivityTimeoutConfig

from .policies import temporal_retry_policy
from .schemas import (
    DocumentFailureInput,
    DocumentIngestionInput,
    PreparedDocument,
    RawCaptureResult,
)

_RECORDS_CANCELLATION_PATCH = "harborrag-document-records-cancellation"
_CAPTURE_TIMEOUT = timedelta(minutes=30)
_PARSE_TIMEOUT = timedelta(minutes=30)
_STAGE_TIMEOUT = timedelta(minutes=60)
_PUBLISH_TIMEOUT = timedelta(minutes=5)


def _start_to_close(timeouts: ActivityTimeoutConfig, own_budget: timedelta) -> timedelta:
    """Return the run's ``ingestion.activity_timeout`` when it sets one, else ``own_budget``."""
    if timeouts.document_seconds is None:
        return own_budget
    return timedelta(seconds=timeouts.document_seconds)


@workflow.defn(name="harborrag.document_ingestion")
class DocumentIngestionWorkflow:
    """Run one independently retryable document through the release stages."""

    @workflow.run
    async def run(self, request: DocumentIngestionInput) -> DocumentIngestionOutcome:
        """Capture, parse, release and publish one document, recording any failure.

        A failed or cancelled activity records the document's failure before the
        workflow ends.

        Args:
            request: The document's plan entry and the run's workflow options.

        Returns:
            The publish outcome, ``FAILED``, or ``SOURCE_ITEM_MISSING`` when the
            source no longer has the item.
        """
        prepared: PreparedDocument | None = None
        failed_stage = "FetchAndCaptureRaw"
        queues = request.workflow_options.task_queues
        discovery_retry = temporal_retry_policy(request.workflow_options.retries.discovery)
        document_retry = temporal_retry_policy(request.workflow_options.retries.document)
        timeouts = request.workflow_options.timeouts
        try:
            capture = await workflow.execute_activity(
                "harborrag.fetch_and_capture_raw",
                request,
                task_queue=queues.io,
                start_to_close_timeout=_start_to_close(timeouts, _CAPTURE_TIMEOUT),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=document_retry,
                result_type=RawCaptureResult,
            )
            failed_stage = "ParseAndNormalize"
            prepared = await workflow.execute_activity(
                "harborrag.parse_and_normalize",
                capture,
                task_queue=queues.parser,
                start_to_close_timeout=_start_to_close(timeouts, _PARSE_TIMEOUT),
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
                        start_to_close_timeout=_start_to_close(timeouts, _STAGE_TIMEOUT),
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
                    start_to_close_timeout=_start_to_close(timeouts, _PUBLISH_TIMEOUT),
                    retry_policy=document_retry,
                    result_type=DocumentIngestionOutcome,
                )
            )
        except asyncio.CancelledError:
            await _record_cancellation(request, prepared, failed_stage)
            raise
        except ActivityError as error:
            if is_cancelled_exception(error) and workflow.patched(_RECORDS_CANCELLATION_PATCH):
                await _record_cancellation(request, prepared, failed_stage)
                raise
            error_type, _ = durable_failure(error)
            await workflow.execute_activity(
                "harborrag.record_document_failure",
                DocumentFailureInput(
                    document=request,
                    prepared=prepared,
                    failed_stage=failed_stage,
                    error_type=error_type,
                    skip_reason=durable_skip_reason(error),
                ),
                task_queue=queues.io,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=discovery_retry,
            )
            if error_type == "source_item_not_found":
                return DocumentIngestionOutcome.SOURCE_ITEM_MISSING
            return DocumentIngestionOutcome.FAILED


async def _record_cancellation(
    request: DocumentIngestionInput, prepared: PreparedDocument | None, failed_stage: str
) -> None:
    """Record the document as cancelled before Temporal closes the run as cancelled.

    Shielded, so a second cancel request cannot stop the recording.
    """
    recording = workflow.execute_activity(
        "harborrag.record_document_failure",
        DocumentFailureInput(
            document=request,
            prepared=prepared,
            failed_stage=failed_stage,
            error_type="cancelled",
        ),
        task_queue=request.workflow_options.task_queues.io,
        start_to_close_timeout=timedelta(minutes=2),
        retry_policy=temporal_retry_policy(request.workflow_options.retries.discovery),
    )
    await asyncio.shield(recording)
