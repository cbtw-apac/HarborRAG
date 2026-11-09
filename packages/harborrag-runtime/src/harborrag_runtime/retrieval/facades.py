"""Reader façades shared by the SDK and reader-only application.

Each façade turns a transport-independent reader request into one
``RuntimeRetrievalService`` call and its response; no retrieval logic lives here.

Start from: RetrievalFacade, KnowledgeFacade, GraphFacade.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Protocol

from harborrag_core.contracts.reader import (
    DocumentContextRequest,
    DocumentContextResponse,
    DocumentListRequest,
    DocumentListResponse,
    EntityFindRequest,
    EntityFindResponse,
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
    RelationSearchRequest,
    RelationSearchResponse,
    RetrievalRequest,
    RetrievalResponse,
    SemanticPathRequest,
    SemanticPathResponse,
    SourceListRequest,
    SourceListResponse,
)
from harborrag_core.indexing import FilterOperator, VectorFilter, VectorFilterCondition
from harborrag_core.security import AccessContext
from harborrag_core.topology.records import CanonicalMention

if TYPE_CHECKING:
    from harborrag_runtime.retrieval.service import RuntimeRetrievalService


class _ReaderOwner(Protocol):
    async def _retrieval_service(self) -> RuntimeRetrievalService: ...


class RetrievalFacade:
    """Passage and entity search.

    Args:
        owner: Runtime that opens the retrieval service on first use.
    """

    def __init__(self, owner: _ReaderOwner) -> None:
        self._owner = owner

    async def search(self, request: RetrievalRequest) -> RetrievalResponse:
        """Search passages for ``request`` and return them with plain-dict diagnostics.

        A malformed range filter raises ``ValueError``.

        Args:
            request: Search to run.

        Returns:
            The passages, diagnostics and topology evidence.
        """
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
                rerank=request.rerank,
            ),
        )
        return RetrievalResponse(
            request_id=report.request_id,
            lane=report.lane,
            results=report.results,
            diagnostics=asdict(report.diagnostics),
            evidence=report.evidence,
        )

    async def find_entities(self, request: EntityFindRequest) -> EntityFindResponse:
        """Rank source entities for ``request``."""
        service = await self._owner._retrieval_service()
        return await service.find_entities(request)


class GraphFacade:
    """Knowledge-graph triplet, path and subgraph search.

    Args:
        owner: Runtime that opens the retrieval service on first use.
    """

    def __init__(self, owner: _ReaderOwner) -> None:
        self._owner = owner

    async def search_triplets(
        self,
        request: GraphTripletRequest,
    ) -> GraphTripletResponse:
        """Return the triplets that match ``request``."""
        service = await self._owner._retrieval_service()
        result = await service.search_graph_triplets(request.query, access=request.access)
        return GraphTripletResponse(
            triplets=result.triplets,
            diagnostics=asdict(result.diagnostics),
        )

    async def find_paths(self, request: GraphPathRequest) -> GraphPathResponse:
        """Return the graph paths that match ``request``."""
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
        """Return the subgraph around ``request``'s seed nodes."""
        service = await self._owner._retrieval_service()
        result = await service.search_graph_subgraph(request.query, access=request.access)
        return GraphSubgraphResponse(
            nodes=result.graph.nodes,
            relations=result.graph.relations,
            diagnostics=asdict(result.diagnostics),
        )


class KnowledgeFacade:
    """Canonical evidence and semantic reads used by MCP and the SDK.

    Args:
        owner: Runtime that opens the retrieval service on first use.
    """

    def __init__(self, owner: _ReaderOwner) -> None:
        self._owner = owner

    async def list_documents(self, request: DocumentListRequest) -> DocumentListResponse:
        """Return one page of the documents the caller can read."""
        service = await self._owner._retrieval_service()
        return await service.list_documents(request)

    async def fetch_evidence(self, request: EvidenceFetchRequest) -> EvidenceFetchResponse:
        """Return the requested chunks as retrieval results."""
        service = await self._owner._retrieval_service()
        return await service.fetch_evidence(request.chunk_ids, access=request.access)

    async def read_evidence(self, request: EvidenceReadRequest) -> EvidenceReadResponse:
        """Return the requested chunks with their provenance and availability."""
        service = await self._owner._retrieval_service()
        return await service.read_evidence(request)

    async def get_document_context(
        self, request: DocumentContextRequest
    ) -> DocumentContextResponse:
        """Return a window of consecutive chunks of one document."""
        service = await self._owner._retrieval_service()
        return await service.get_document_context(request)

    async def list_sources(self, request: SourceListRequest) -> SourceListResponse:
        """Return one page of the source scopes the caller can read."""
        service = await self._owner._retrieval_service()
        return await service.list_readable_sources(request)

    async def resolve_graph_nodes(
        self, request: GraphNodeResolveRequest
    ) -> GraphNodeResolveResponse:
        """Return the graph nodes that ``request``'s selector matches."""
        service = await self._owner._retrieval_service()
        return await service.resolve_graph_nodes(request)

    async def resolve_entities(self, request: EntityResolveRequest) -> EntityResolveResponse:
        """Return the canonical mentions of the named entity."""
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
        """Return the canonical mentions of the given entities or chunks."""
        service = await self._owner._retrieval_service()
        return await service.lookup_entities(
            entity_ids=entity_ids,
            chunk_ids=chunk_ids,
            access=access,
        )

    async def find_relations(self, request: RelationSearchRequest) -> RelationSearchResponse:
        """Return the assertions that connect one canonical entity to others."""
        service = await self._owner._retrieval_service()
        return await service.find_semantic_relations(
            request.entity_id,
            predicates=request.predicates,
            direction=request.direction,
            limit=request.limit,
            access=request.access,
        )

    async def find_paths(self, request: SemanticPathRequest) -> SemanticPathResponse:
        """Return the assertion paths between two canonical entities."""
        service = await self._owner._retrieval_service()
        return await service.find_semantic_paths(request)


_RANGE_OPERATORS = {
    "gte": FilterOperator.GREATER_THAN_OR_EQUAL,
    "gt": FilterOperator.GREATER_THAN,
    "lte": FilterOperator.LESS_THAN_OR_EQUAL,
    "lt": FilterOperator.LESS_THAN,
}


def _build_vector_filter(filters: dict[str, object]) -> VectorFilter | None:
    """Map ``{"field": value}`` onto payload conditions.

    A scalar is an equality, a list is set membership, and a mapping of bounds
    (``{"fields.years_of_experience": {"gte": 3}}``) is a range on a number.
    """
    if not filters:
        return None
    return VectorFilter(
        must=[
            condition
            for name, value in sorted(filters.items())
            for condition in _conditions(name, value)
        ]
    )


def _conditions(name: str, value: object) -> list[VectorFilterCondition]:
    if isinstance(value, Mapping):
        conditions = []
        for bound, raw in value.items():
            operator = _RANGE_OPERATORS.get(str(bound))
            if operator is None or isinstance(raw, bool) or not isinstance(raw, int | float):
                raise ValueError(
                    f"filter {name!r} range bound {bound!r} must be one of "
                    f"{', '.join(_RANGE_OPERATORS)} with a number"
                )
            conditions.append(VectorFilterCondition(field=name, operator=operator, value=raw))
        if not conditions:
            raise ValueError(f"filter {name!r} range has no bounds")
        return conditions
    operator = FilterOperator.IN if isinstance(value, list | tuple) else FilterOperator.EQUALS
    return [VectorFilterCondition(field=name, operator=operator, value=value)]
