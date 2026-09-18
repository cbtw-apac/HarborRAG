"""Narrow reader service ports consumed by shared tools."""

from __future__ import annotations

from typing import Protocol

from harborrag_core.contracts import reader as dto


class RetrievalReader(Protocol):
    async def search(self, request: dto.RetrievalRequest) -> dto.RetrievalResponse: ...


class GraphReader(Protocol):
    async def search_triplets(
        self, request: dto.GraphTripletRequest
    ) -> dto.GraphTripletResponse: ...
    async def find_paths(self, request: dto.GraphPathRequest) -> dto.GraphPathResponse: ...
    async def expand_subgraph(
        self, request: dto.GraphSubgraphRequest
    ) -> dto.GraphSubgraphResponse: ...


class KnowledgeReader(Protocol):
    async def read_evidence(self, request: dto.EvidenceReadRequest) -> dto.EvidenceReadResponse: ...
    async def get_document_context(
        self, request: dto.DocumentContextRequest
    ) -> dto.DocumentContextResponse: ...
    async def list_sources(self, request: dto.SourceListRequest) -> dto.SourceListResponse: ...
    async def resolve_graph_nodes(
        self, request: dto.GraphNodeResolveRequest
    ) -> dto.GraphNodeResolveResponse: ...
    async def list_documents(
        self, request: dto.DocumentListRequest
    ) -> dto.DocumentListResponse: ...
    async def get_document_metadata(
        self, request: dto.DocumentMetadataRequest
    ) -> dto.DocumentMetadataResponse: ...


class ReaderServices(Protocol):
    retrieval: RetrievalReader
    graph: GraphReader
    knowledge: KnowledgeReader
