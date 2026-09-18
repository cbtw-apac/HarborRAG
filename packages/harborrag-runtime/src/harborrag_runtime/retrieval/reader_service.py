"""Public reader operations delegated by the retrieval service."""

from __future__ import annotations

from harborrag_core.contracts.reader import (
    DocumentContextRequest,
    DocumentContextResponse,
    DocumentListRequest,
    DocumentListResponse,
    DocumentMetadataRequest,
    DocumentMetadataResponse,
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

    async def get_document_metadata(
        self, request: DocumentMetadataRequest
    ) -> DocumentMetadataResponse:
        return await self._reader.document_metadata(request)

    async def list_documents(self, request: DocumentListRequest) -> DocumentListResponse:
        return await self._reader.list_documents(request)

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
