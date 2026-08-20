"""Ports, policies, and value objects for runtime retrieval."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from harborrag_adapters.repositories.object_store import ChunkArtifactReader
from harborrag_adapters.repositories.vector.base import HarborVectorRepository
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorFilter
from harborrag_core.ingestion import ActiveDocumentVersion, KnowledgeGraphTraversal
from harborrag_core.ports.model_clients import AsyncHarborEmbedClientProtocol
from harborrag_core.retrieval import (
    GraphPathQuery,
    GraphPathResult,
    GraphSubgraphQuery,
    GraphTripletQuery,
    GraphTripletResult,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.ingestion.representations import BM25SparseEncoder
from harborrag_engine.retrieval import RetrievalLane


class ActiveVersionResolver(Protocol):
    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> Mapping[str, ActiveDocumentVersion]: ...


class RetrievalTelemetry(Protocol):
    """Minimal metrics surface consumed by retrieval."""

    def record_stale_candidate_rejections(self, count: int) -> None: ...


class KnowledgeGraphReader(Protocol):
    async def traverse(
        self,
        start_node_key: str,
        *,
        max_depth: int,
        max_nodes: int,
        direction: str,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal: ...

    async def search_triplets(
        self,
        query: GraphTripletQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphTripletResult: ...

    async def find_paths(
        self,
        query: GraphPathQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphPathResult: ...

    async def expand_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal: ...


@dataclass(frozen=True, slots=True)
class GraphResultNeighborhood:
    """The 2-hop graph neighborhood discovered from one vector result, scoped to a document.

    ``nodes``/``relations`` use the same compact shape as the graph search tools
    (node_key/node_kind/entity_type/title, relation_type/source_node_key/target_node_key)
    so a caller can render "how did this result connect to the graph" directly.
    """

    result_id: str
    nodes: tuple[dict[str, object], ...] = ()
    relations: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class GraphDocumentSummary:
    """One document the retrieved chunks belong to, with the sections they came from."""

    document_id: str
    title: str | None = None
    sections: tuple[str, ...] = ()
    related_results: tuple[GraphResultNeighborhood, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalDiagnostics:
    candidate_hits: int
    stale_candidates: int
    unpublished_candidates: int
    malformed_candidates: int
    search_window: int
    graph_nodes: int
    graph_relations: int
    graph_truncated: bool
    duration_ms: float
    # Structural provenance for the results, empty unless graph observation ran. Kept
    # last with a default so the positional shape of the existing fields is unchanged.
    graph_documents: tuple[GraphDocumentSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeRetrievalReport:
    request_id: str
    lane: RetrievalLane
    results: tuple[RetrievalResult, ...]
    diagnostics: RetrievalDiagnostics


@dataclass(frozen=True, slots=True)
class RetrievalResources:
    embed_client: AsyncHarborEmbedClientProtocol
    vector_repository: HarborVectorRepository
    active_versions: ActiveVersionResolver
    chunk_reader: ChunkArtifactReader
    sparse_encoder: BM25SparseEncoder
    graph_repository: KnowledgeGraphReader | None = None


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    embedding_model: str
    embedding_dimensions: int
    normalize_embeddings: bool = True
    dense_weight: float = 0.7

    def __post_init__(self) -> None:
        if not self.embedding_model.strip():
            raise ValueError("retrieval embedding model must be non-empty")
        if self.embedding_dimensions < 1:
            raise ValueError("retrieval embedding dimensions must be positive")
        if not 0 <= self.dense_weight <= 1:
            raise ValueError("retrieval dense weight must be between zero and one")


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    lane: RetrievalLane = RetrievalLane.HYBRID
    filters: VectorFilter | None = None
    observe_graph: bool = False


CloseOperation = Callable[[], Awaitable[None]]
