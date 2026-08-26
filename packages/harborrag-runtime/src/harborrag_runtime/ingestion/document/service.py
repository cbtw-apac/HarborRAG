"""Application service for publishing one document release pipeline."""

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.harbor_connector import HarborConnector
from harborrag_core.storage import StorageOperationContext

from .dependencies import DocumentReleaseDependencies
from .models import DocumentReleaseOutcome, DocumentReleaseRequest
from .pipeline import DocumentStagePipeline


class DocumentReleaseService:
    """Publish one document version through immutable, verifiable projections."""

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
        connector: BaseConnector | HarborConnector,
    ) -> DocumentReleaseOutcome:
        return await self._pipeline.release(request, connector)

    async def replay(
        self,
        request: DocumentReleaseRequest,
        document_version_id: str,
    ) -> DocumentReleaseOutcome:
        """Resume a failed release solely from durable ingestion artifacts."""

        return await self._pipeline.replay(request, document_version_id)
