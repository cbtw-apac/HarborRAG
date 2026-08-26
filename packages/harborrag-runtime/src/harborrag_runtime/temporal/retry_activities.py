from __future__ import annotations

from temporalio import activity

from harborrag_core.ingestion import DocumentIngestionOutcome
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.ingestion.composition import IngestionRuntime
from harborrag_runtime.ingestion.source.models import SourceDispatchSummary
from harborrag_runtime.ingestion.source.tasks import source_scan_id

from .activity_observability import ActivityObservability
from .conversion import to_workflow_artifact
from .plan_resolver import PlanDocumentResolver
from .schemas import (
    RetryDocumentFailureInput,
    RetryDocumentInput,
    RetryFailuresInput,
    RetryFinalizationInput,
    RetryPreparationResult,
    RetryTaskFailureInput,
)


class RetryActivitiesMixin:
    """Temporal boundaries for failed-document retry workflows."""

    _runtime: IngestionRuntime
    _observability: ActivityObservability
    _documents: PlanDocumentResolver

    @activity.defn(name="harborrag.prepare_retry_failures")
    async def prepare_retry_failures(
        self,
        request: RetryFailuresInput,
    ) -> RetryPreparationResult:
        with self._observability.boundary("PrepareRetryFailures"):
            context = StorageOperationContext.system(request.tenant_id)
            original = await self._runtime.source_plans.find(
                task_id=request.original_task_id,
                scan_id=source_scan_id(request.original_task_id),
                context=context,
            )
            if original is None:
                raise ValueError("original ingestion source plan is unavailable")
            planned = await self._runtime.source_plans.get(original, context=context)
            selected_ids = set(request.document_ids)
            selected = tuple(item for item in planned if item.document_id in selected_ids)
            if len(selected) != len(selected_ids):
                raise ValueError("retry request contains a document outside the source plan")
            reference = await self._runtime.source_plans.put(
                task_id=request.retry_task_id,
                scan_id=source_scan_id(request.retry_task_id),
                planned=selected,
                context=context,
            )
            await self._runtime.sources.begin_retry(
                request.retry_task_id,
                selected=len(selected),
            )
            return RetryPreparationResult(
                plan_reference=to_workflow_artifact(reference),
                document_count=len(selected),
            )

    @activity.defn(name="harborrag.retry_document_release")
    async def retry_document_release(
        self,
        request: RetryDocumentInput,
    ) -> DocumentIngestionOutcome:
        with self._observability.boundary("RetryDocumentRelease"):
            planned = await self._documents.get(request)
            return await self._runtime.sources.retry_one(
                retry_task_id=request.retry_task_id,
                original_task_id=request.original_task_id,
                planned=planned,
                connector_factory=lambda: self._runtime.connector(
                    planned.request.connector_name,
                    configuration_fingerprint=(planned.request.configuration_fingerprint),
                ),
            )

    @activity.defn(name="harborrag.record_retry_document_failure")
    async def record_retry_document_failure(
        self,
        request: RetryDocumentFailureInput,
    ) -> None:
        with self._observability.boundary("RecordRetryDocumentFailure"):
            planned = await self._documents.get(request.document)
            await self._runtime.sources.record_retry_failure(
                retry_task_id=request.document.retry_task_id,
                original_task_id=request.document.original_task_id,
                planned=planned,
                error_type=request.error_type,
            )

    @activity.defn(name="harborrag.record_retry_failures_task_failure")
    async def record_retry_failures_task_failure(
        self,
        request: RetryTaskFailureInput,
    ) -> None:
        with self._observability.boundary("RecordRetryFailuresTaskFailure"):
            await self._runtime.sources.fail_retry(
                request.retry_task_id,
                error_code=request.error_code,
            )

    @activity.defn(name="harborrag.finalize_retry_failures")
    async def finalize_retry_failures(self, request: RetryFinalizationInput) -> None:
        with self._observability.boundary("FinalizeRetryFailures"):
            await self._runtime.sources.finish_retry(
                request.retry_task_id,
                selected=request.selected,
                summary=SourceDispatchSummary(
                    published=request.summary.published,
                    unchanged=request.summary.unchanged,
                    failed=request.summary.failed,
                ),
            )
