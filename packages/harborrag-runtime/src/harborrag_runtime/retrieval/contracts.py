"""Ports, policies, and value objects for runtime retrieval."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from harborrag_adapters.repositories.object_store import ChunkArtifactReader
from harborrag_adapters.repositories.vector.base import HarborVectorRepository
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorFilter
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    DocumentVersionSnapshot,
    KnowledgeGraphTraversal,
    ReadableSource,
    SourceCatalogQuery,
)
from harborrag_core.ports.model_clients import AsyncHarborEmbedClientProtocol
from harborrag_core.ports.summary_projection import SummaryReaderPort
from harborrag_core.retrieval import (
    GraphNodeResolutionQuery,
    GraphNodeResolutionResult,
    GraphPathQuery,
    GraphPathResult,
    GraphSubgraphQuery,
    GraphTripletQuery,
    GraphTripletResult,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.retrieval_policy import TopologyRetrievalPolicy
from harborrag_core.topology.search import (
    ContextualSearchPort,
    EvidenceBundle,
    RetrievalMode,
    TopologyDiagnostics,
    TopologySearchPort,
)
from harborrag_engine.ingestion.representations import BM25SparseEncoder
from harborrag_engine.retrieval import RetrievalLane


class ActiveVersionResolver(Protocol):
    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> Mapping[str, ActiveDocumentVersion]: ...


class DocumentSnapshotReader(Protocol):
    async def active_snapshot(self, document_id: str) -> DocumentVersionSnapshot | None: ...


class SourceCatalogReader(Protocol):
    async def list_readable_sources(
        self,
        query: SourceCatalogQuery,
    ) -> tuple[ReadableSource, ...]: ...


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

    async def resolve_nodes(
        self,
        query: GraphNodeResolutionQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphNodeResolutionResult: ...


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
    short_by: int = 0
    topology: TopologyDiagnostics = field(default_factory=TopologyDiagnostics)


@dataclass(frozen=True, slots=True)
class RuntimeRetrievalReport:
    request_id: str
    lane: RetrievalLane
    results: tuple[RetrievalResult, ...]
    diagnostics: RetrievalDiagnostics
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)


@dataclass(frozen=True, slots=True)
class RetrievalResources:
    embed_client: AsyncHarborEmbedClientProtocol
    vector_repository: HarborVectorRepository
    active_versions: ActiveVersionResolver
    chunk_reader: ChunkArtifactReader
    sparse_encoder: BM25SparseEncoder
    graph_repository: KnowledgeGraphReader | None = None
    topology_repository: TopologySearchPort | None = None
    contextual_search: ContextualSearchPort | None = None
    document_snapshots: DocumentSnapshotReader | None = None
    source_catalog: SourceCatalogReader | None = None
    summary_repository: SummaryReaderPort | None = None


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    embedding_model: str
    embedding_dimensions: int
    normalize_embeddings: bool = True
    dense_weight: float = 0.7
    semantic_weight: float = 0.5
    topology: TopologyRetrievalPolicy = field(default_factory=TopologyRetrievalPolicy)

    def __post_init__(self) -> None:
        if not self.embedding_model.strip():
            raise ValueError("retrieval embedding model must be non-empty")
        if self.embedding_dimensions < 1:
            raise ValueError("retrieval embedding dimensions must be positive")
        if not 0 <= self.dense_weight <= 1:
            raise ValueError("retrieval dense weight must be between zero and one")
        if not 0 <= self.semantic_weight <= 1:
            raise ValueError("retrieval semantic weight must be between zero and one")


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    lane: RetrievalLane = RetrievalLane.HYBRID
    filters: VectorFilter | None = None
    observe_graph: bool = False
    # Extra graph observation seeds; see ``RetrievalRequest.graph_seed_node_keys``.
    graph_seeds: tuple[str, ...] = ()
    mode: RetrievalMode = RetrievalMode.FLAT

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", RetrievalMode(self.mode))


CloseOperation = Callable[[], Awaitable[None]]
