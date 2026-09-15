"""Public reader operations delegated by the retrieval service."""

from __future__ import annotations

from ..contracts import (
    DocumentContextRequest,
    DocumentContextResponse,
    EvidenceReadRequest,
    EvidenceReadResponse,
    GraphNodeResolveRequest,
    GraphNodeResolveResponse,
    SourceListRequest,
    SourceListResponse,
)
from .readers import ReaderRetrieval


class RuntimeReaderRetrievalMixin:
    _reader: ReaderRetrieval

    async def read_evidence(self, request: EvidenceReadRequest) -> EvidenceReadResponse:
        return await self._reader.read_evidence(request)

    async def get_document_context(
        self, request: DocumentContextRequest
    ) -> DocumentContextResponse:
        return await self._reader.document_context(request)

    async def list_readable_sources(self, request: SourceListRequest) -> SourceListResponse:
        return await self._reader.list_sources(request)

    async def resolve_graph_nodes(
        self, request: GraphNodeResolveRequest
    ) -> GraphNodeResolveResponse:
        return await self._reader.resolve_graph_nodes(request)


__all__ = ["RuntimeReaderRetrievalMixin"]
