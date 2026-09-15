"""Bounded document discovery with current publication and permission checks."""

from __future__ import annotations

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.reader_contracts import (
    DocumentListRequest,
    DocumentListResponse,
    DocumentMetadata,
)

from .reader_resources import ReaderResources


class DocumentCatalogReader:
    def __init__(self, resources: ReaderResources) -> None:
        self._resources = resources

    async def metadata(
        self, document_id: str, access: AccessContext, context: StorageOperationContext
    ) -> DocumentMetadata | None:
        resources = self._resources
        if resources.topology is None or resources.snapshots is None:
            raise HarborCapabilityError("document catalog is not configured")
        if not await self._allowed(document_id, access):
            return None
        snapshot = await resources.snapshots.active_snapshot(document_id)
        if (
            snapshot is None
            or snapshot.chunk_artifact is None
            or snapshot.chunk_index_artifact is None
        ):
            return None
        artifacts = await resources.chunks.get_artifacts(
            snapshot.chunk_artifact, snapshot.chunk_index_artifact, context=context
        )
        if not artifacts.entries:
            return None
        chunk = await resources.chunks.get_chunk(
            artifacts, artifacts.entries[0].chunk_id, context=context
        )
        version_id = str(snapshot.document_version_id)
        if (
            str(chunk.tenant_id) != str(access.tenant_id)
            or str(chunk.document_id) != document_id
            or str(chunk.document_version_id) != version_id
        ):
            return None
        current = await resources.snapshots.active_snapshot(document_id)
        if (
            current is None
            or str(current.document_version_id) != version_id
            or not await self._allowed(document_id, access)
        ):
            return None
        return DocumentMetadata(
            document_id=document_id,
            document_version_id=version_id,
            title=chunk.hierarchy.document_title,
            source_scope_id=chunk.source_scope_id,
            connector_type=chunk.connector_type.value,
            chunk_count=len(artifacts.entries),
        )

    async def list_documents(
        self,
        request: DocumentListRequest,
        request_id: str,
        context: StorageOperationContext,
    ) -> DocumentListResponse:
        topology = self._resources.topology
        if topology is None:
            raise HarborCapabilityError("document catalog is not configured")
        # The existing permission port has a strict 10,000-document enumeration
        # budget and fails explicitly when exceeded. Only one page reads artifacts.
        document_ids = await topology.allowed_document_ids(
            str(request.access.tenant_id), access=request.access, limit=10_000
        )
        candidates = sorted(
            item for item in document_ids if item > (request.after_document_id or "")
        )
        page = candidates[: request.limit]
        documents: list[DocumentMetadata] = []
        for document_id in page:
            item = await self.metadata(document_id, request.access, context)
            if item is not None:
                documents.append(item)
        next_id = page[-1] if len(candidates) > request.limit else None
        return DocumentListResponse(request_id, tuple(documents), next_id)

    async def _allowed(self, document_id: str, access: AccessContext) -> bool:
        topology = self._resources.topology
        if topology is None:
            return False
        allowed = await topology.authorized_document_ids(
            str(access.tenant_id), (document_id,), access=access
        )
        return document_id in allowed
