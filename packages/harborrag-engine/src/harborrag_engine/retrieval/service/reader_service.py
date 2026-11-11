"""Public reader operations delegated by the retrieval service."""

from __future__ import annotations

from harborrag_core.contracts.reader import (
    DocumentContextRequest,
    DocumentContextResponse,
    DocumentListRequest,
    DocumentListResponse,
    EvidenceReadRequest,
    EvidenceReadResponse,
    GraphNodeResolveRequest,
    GraphNodeResolveResponse,
    SourceListRequest,
    SourceListResponse,
)

from .readers import ReaderRetrieval


class RuntimeReaderRetrievalMixin:
    """Expose the bounded reader operations of ``ReaderRetrieval`` on the retrieval service."""

    _reader: ReaderRetrieval

    async def list_documents(self, request: DocumentListRequest) -> DocumentListResponse:
        """List readable documents."""
        return await self._reader.list_documents(request)

    async def read_evidence(self, request: EvidenceReadRequest) -> EvidenceReadResponse:
        """Read authorized immutable evidence."""
        return await self._reader.read_evidence(request)

    async def get_document_context(
        self, request: DocumentContextRequest
    ) -> DocumentContextResponse:
        """Read ordered document context."""
        return await self._reader.document_context(request)

    async def list_readable_sources(self, request: SourceListRequest) -> SourceListResponse:
        """List the sources the caller may read."""
        return await self._reader.list_sources(request)

    async def resolve_graph_nodes(
        self, request: GraphNodeResolveRequest
    ) -> GraphNodeResolveResponse:
        """Resolve graph nodes the caller may see."""
        return await self._reader.resolve_graph_nodes(request)
