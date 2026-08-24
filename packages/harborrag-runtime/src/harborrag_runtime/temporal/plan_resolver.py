from __future__ import annotations

from temporalio.exceptions import ApplicationError

from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.ingestion.source.models import PlannedDocumentRelease
from harborrag_runtime.ingestion.source.plan import SourcePlanRepository

from .conversion import to_artifact_reference
from .schemas import DocumentIngestionInput, RetryDocumentInput


class InvalidDocumentIndexError(ApplicationError):
    """Raised when a plan document index is negative or out of range."""

    def __init__(self) -> None:
        super().__init__("source plan document index is invalid", non_retryable=True)


class PlanDocumentResolver:
    """Resolve one bounded document from an immutable source dispatch plan."""

    def __init__(self, plans: SourcePlanRepository) -> None:
        self._plans = plans

    async def get(
        self,
        request: DocumentIngestionInput | RetryDocumentInput,
    ) -> PlannedDocumentRelease:
        planned = await self._plans.get(
            to_artifact_reference(request.plan_reference),
            context=StorageOperationContext.system(request.tenant_id),
        )
        if request.document_index < 0:
            raise InvalidDocumentIndexError()
        try:
            return planned[request.document_index]
        except IndexError as error:
            raise InvalidDocumentIndexError() from error
