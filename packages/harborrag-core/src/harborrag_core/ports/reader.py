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
    """The three reader ports a tool catalog is built over.

    Read-only properties, not mutable attributes: a mutable protocol member is
    invariant, so a provider exposing a *narrower* ``graph`` -- a concrete
    façade rather than the bare port -- failed to satisfy it even though every
    method matched. Nothing assigns through this protocol; declaring the
    members read-only says so and makes them covariant.
    """

    @property
    def retrieval(self) -> RetrievalReader: ...
    @property
    def graph(self) -> GraphReader: ...
    @property
    def knowledge(self) -> KnowledgeReader: ...
