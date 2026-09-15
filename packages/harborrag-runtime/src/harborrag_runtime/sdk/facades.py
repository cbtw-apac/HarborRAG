"""Narrow SDK façades for ingestion and retrieval."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from harborrag_core.indexing import FilterOperator, VectorFilter, VectorFilterCondition
from harborrag_core.security import AccessContext
from harborrag_core.topology.records import CanonicalMention
from harborrag_runtime.reader_contracts import (
    DocumentListRequest,
    DocumentListResponse,
    DocumentMetadataRequest,
    DocumentMetadataResponse,
)

from ..contracts import (
    DocumentContextRequest,
    DocumentContextResponse,
    EntityResolveRequest,
    EntityResolveResponse,
    EvidenceFetchRequest,
    EvidenceFetchResponse,
    EvidenceReadRequest,
    EvidenceReadResponse,
    GraphNodeResolveRequest,
    GraphNodeResolveResponse,
    GraphPathRequest,
    GraphPathResponse,
    GraphSubgraphRequest,
    GraphSubgraphResponse,
    GraphTripletRequest,
    GraphTripletResponse,
    IngestionRequest,
    IngestionResult,
    IngestionStatus,
    IngestionTaskReference,
    RelationSearchRequest,
    RelationSearchResponse,
    RetrievalRequest,
    RetrievalResponse,
    SemanticPathRequest,
    SemanticPathResponse,
    SourceListRequest,
    SourceListResponse,
)

if TYPE_CHECKING:
    from .runtime import HarborRAG


class IngestionFacade:
    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def run(self, request: IngestionRequest) -> IngestionResult:
        return await self._owner._ingestion_run(request)

    async def submit(self, request: IngestionRequest) -> IngestionTaskReference:
        return await self._owner._ingestion_submit(request)

    async def status(self, task_id: str) -> IngestionStatus:
        return await self._owner._ingestion_status(task_id)

    async def pause(self, task_id: str) -> None:
        await self._owner._ingestion_control(task_id, "pause")

    async def resume(self, task_id: str) -> None:
        await self._owner._ingestion_control(task_id, "resume")

    async def cancel(self, task_id: str) -> None:
        await self._owner._ingestion_control(task_id, "cancel")


class RetrievalFacade:
    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def search(self, request: RetrievalRequest) -> RetrievalResponse:
        from ..retrieval import RetrievalOptions

        service = await self._owner._retrieval_service()
        report = await service.retrieve(
            request.query,
            tenant_id=str(request.access.tenant_id),
            top_k=request.top_k,
            access=request.access,
            options=RetrievalOptions(
                lane=request.lane,
                filters=_build_vector_filter(request.filters),
                observe_graph=request.observe_graph,
                graph_seeds=request.graph_seed_node_keys,
                mode=request.mode,
            ),
        )
        return RetrievalResponse(
            request_id=report.request_id,
            lane=report.lane,
            results=report.results,
            diagnostics=asdict(report.diagnostics),
            evidence=report.evidence,
        )


class GraphFacade:
    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def search_triplets(
        self,
        request: GraphTripletRequest,
    ) -> GraphTripletResponse:
        service = await self._owner._retrieval_service()
        result = await service.search_graph_triplets(request.query, access=request.access)
        return GraphTripletResponse(
            triplets=result.triplets,
            diagnostics=asdict(result.diagnostics),
        )

    async def find_paths(self, request: GraphPathRequest) -> GraphPathResponse:
        service = await self._owner._retrieval_service()
        result = await service.search_graph_paths(request.query, access=request.access)
        return GraphPathResponse(
            paths=result.paths,
            diagnostics=asdict(result.diagnostics),
        )

    async def expand_subgraph(
        self,
        request: GraphSubgraphRequest,
    ) -> GraphSubgraphResponse:
        service = await self._owner._retrieval_service()
        result = await service.search_graph_subgraph(request.query, access=request.access)
        return GraphSubgraphResponse(
            nodes=result.graph.nodes,
            relations=result.graph.relations,
            diagnostics=asdict(result.diagnostics),
        )


class KnowledgeFacade:
    """Canonical evidence and semantic reads used by MCP and agents."""

    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def get_document_metadata(
        self, request: DocumentMetadataRequest
    ) -> DocumentMetadataResponse:
        service = await self._owner._retrieval_service()
        return await service.get_document_metadata(request)

    async def list_documents(self, request: DocumentListRequest) -> DocumentListResponse:
        service = await self._owner._retrieval_service()
        return await service.list_documents(request)

    async def fetch_evidence(self, request: EvidenceFetchRequest) -> EvidenceFetchResponse:
        service = await self._owner._retrieval_service()
        return await service.fetch_evidence(request.chunk_ids, access=request.access)

    async def read_evidence(self, request: EvidenceReadRequest) -> EvidenceReadResponse:
        service = await self._owner._retrieval_service()
        return await service.read_evidence(request)

    async def get_document_context(
        self, request: DocumentContextRequest
    ) -> DocumentContextResponse:
        service = await self._owner._retrieval_service()
        return await service.get_document_context(request)

    async def list_sources(self, request: SourceListRequest) -> SourceListResponse:
        service = await self._owner._retrieval_service()
        return await service.list_readable_sources(request)

    async def resolve_graph_nodes(
        self, request: GraphNodeResolveRequest
    ) -> GraphNodeResolveResponse:
        service = await self._owner._retrieval_service()
        return await service.resolve_graph_nodes(request)

    async def resolve_entities(self, request: EntityResolveRequest) -> EntityResolveResponse:
        service = await self._owner._retrieval_service()
        return await service.resolve_entities(
            request.name,
            limit=request.limit,
            access=request.access,
        )

    async def lookup_entities(
        self,
        *,
        access: AccessContext,
        entity_ids: tuple[str, ...] = (),
        chunk_ids: tuple[str, ...] = (),
    ) -> tuple[CanonicalMention, ...]:
        service = await self._owner._retrieval_service()
        return await service.lookup_entities(
            entity_ids=entity_ids,
            chunk_ids=chunk_ids,
            access=access,
        )

    async def find_relations(self, request: RelationSearchRequest) -> RelationSearchResponse:
        service = await self._owner._retrieval_service()
        return await service.find_semantic_relations(
            request.entity_id,
            predicates=request.predicates,
            direction=request.direction,
            limit=request.limit,
            access=request.access,
        )

    async def find_paths(self, request: SemanticPathRequest) -> SemanticPathResponse:
        service = await self._owner._retrieval_service()
        return await service.find_semantic_paths(request)


def _build_vector_filter(filters: dict[str, object]) -> VectorFilter | None:
    if not filters:
        return None
    return VectorFilter(
        must=[
            VectorFilterCondition(
                field=name,
                operator=FilterOperator.IN
                if isinstance(value, list | tuple)
                else FilterOperator.EQUALS,
                value=value,
            )
            for name, value in sorted(filters.items())
        ]
    )
