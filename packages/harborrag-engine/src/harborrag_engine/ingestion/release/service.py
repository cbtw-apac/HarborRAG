"""Application service for publishing one document release pipeline."""

from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion.release.dependencies import (
    DocumentReleaseDependencies,
    RawDocumentLoader,
)
from harborrag_engine.ingestion.release.models import DocumentReleaseOutcome, DocumentReleaseRequest

from .pipeline import DocumentStagePipeline


class DocumentReleaseService:
    """Publish one document version through immutable, verifiable projections.

    Args:
        dependencies: Ports and stores the release stages read and write.
        pipeline: Stage pipeline to run; built from ``dependencies`` when omitted.
    """

    def __init__(
        self,
        dependencies: DocumentReleaseDependencies,
        *,
        pipeline: DocumentStagePipeline | None = None,
    ) -> None:
        self._dependencies = dependencies
        self._pipeline = pipeline or DocumentStagePipeline(dependencies)

    async def provision(self, *, tenant_id: str) -> None:
        """Provision idempotent projection structures before worker polling."""
        await self._dependencies.vector_store.provision(
            context=StorageOperationContext.system(tenant_id)
        )

    async def release(
        self,
        request: DocumentReleaseRequest,
        connector: RawDocumentLoader,
    ) -> DocumentReleaseOutcome:
        """Load one source item through ``connector`` and release it through the pipeline."""
        return await self._pipeline.release(request, connector)

    async def replay(
        self,
        request: DocumentReleaseRequest,
        document_version_id: str,
    ) -> DocumentReleaseOutcome:
        """Resume a failed release solely from durable ingestion artifacts."""
        return await self._pipeline.replay(request, document_version_id)
