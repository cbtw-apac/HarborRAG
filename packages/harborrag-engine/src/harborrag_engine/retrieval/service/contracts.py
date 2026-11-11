"""Ports, policies, and value objects for runtime retrieval.

The read-side ports the retrieval service needs from storage, the policy it runs
with, and the report it returns. Values only: no I/O and no adapter imports.

Start from: RetrievalResources, RetrievalPolicy, RetrievalOptions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorFilter
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    DocumentVersionSnapshot,
    KnowledgeGraphTraversal,
    ReadableSource,
    SourceCatalogQuery,
    SparseEncoding,
)
from harborrag_core.ports.artifacts import ChunkArtifactReaderPort
from harborrag_core.ports.model_clients import (
    AsyncHarborEmbedClientProtocol,
    AsyncHarborRerankClientProtocol,
)
from harborrag_core.ports.storage import VectorRepositoryPort
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
from harborrag_engine.retrieval import RetrievalLane

from .entity_summary import EntitySummarySearch


class ActiveVersionResolver(Protocol):
    """Postgres-authoritative active version of each logical document."""

    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> Mapping[str, ActiveDocumentVersion]:
        """Return the active version of each document that has one.

        A document missing from the mapping has no published version, so its
        candidates count as unpublished; one with another version counts as stale.
        """
        ...


class DocumentSnapshotReader(Protocol):
    """Artifacts of a document's active version, for the reader tools."""

    async def active_snapshot(self, document_id: str) -> DocumentVersionSnapshot | None:
        """Return the active version's snapshot, or ``None`` when there is none."""
        ...


class SourceCatalogReader(Protocol):
    """Source scopes a caller can read, for the reader tools."""

    async def list_readable_sources(
        self,
        query: SourceCatalogQuery,
    ) -> tuple[ReadableSource, ...]:
        """Return the source scopes that ``query`` selects and its caller may read."""
        ...


class SparseQueryEncoder(Protocol):
    """Sparse term encoding of a query for the sparse lane."""

    def encode_query(self, text: str) -> SparseEncoding:
        """Return the sparse encoding of ``text`` under the index's encoder profile."""
        ...


class RetrievalTelemetry(Protocol):
    """Minimal metrics surface consumed by retrieval."""

    def record_stale_candidate_rejections(self, count: int) -> None:
        """Count candidates dropped because their document version is no longer active."""
        ...


class KnowledgeGraphReader(Protocol):
    """Read-only knowledge graph, scoped by each call's storage context."""

    async def traverse(
        self,
        start_node_key: str,
        *,
        max_depth: int,
        max_nodes: int,
        direction: str,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal:
        """Return the nodes and relations reachable from one node, within the bounds."""
        ...

    async def search_triplets(
        self,
        query: GraphTripletQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphTripletResult:
        """Return the triplets that match ``query``."""
        ...

    async def find_paths(
        self,
        query: GraphPathQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphPathResult:
        """Return the paths between the nodes ``query`` names."""
        ...

    async def expand_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        context: StorageOperationContext,
    ) -> KnowledgeGraphTraversal:
        """Return the neighbourhood of ``query``'s seed nodes."""
        ...

    async def resolve_nodes(
        self,
        query: GraphNodeResolutionQuery,
        *,
        context: StorageOperationContext,
    ) -> GraphNodeResolutionResult:
        """Return the graph nodes that ``query``'s selector matches."""
        ...


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
    """What one retrieval found and dropped, for operators.

    Args:
        candidate_hits: Candidates the vector search returned.
        stale_candidates: Candidates dropped because their document has another
            active version.
        unpublished_candidates: Candidates dropped because their document has no
            published version.
        malformed_candidates: Candidates dropped because their payload was malformed
            or could not be loaded.
        search_window: Vector hits the search asked for to fill the page.
        graph_nodes: Nodes the graph observation found.
        graph_relations: Relations the graph observation found.
        graph_truncated: The graph observation hit its bounds.
        duration_ms: Wall time of the retrieval, in milliseconds.
        graph_documents: Structural provenance for the results, empty unless graph
            observation ran. Kept after the required fields with a default, so their
            positional shape is unchanged.
        short_by: How many fewer results than ``top_k`` were returned.
        topology: Topology evidence selection counts.
        duplicates_collapsed: Hits dropped because a better-ranked hit carried the same
            text (same ``content_hash``); the search window refilled the page behind them.
    """

    candidate_hits: int
    stale_candidates: int
    unpublished_candidates: int
    malformed_candidates: int
    search_window: int
    graph_nodes: int
    graph_relations: int
    graph_truncated: bool
    duration_ms: float
    graph_documents: tuple[GraphDocumentSummary, ...] = ()
    short_by: int = 0
    topology: TopologyDiagnostics = field(default_factory=TopologyDiagnostics)
    duplicates_collapsed: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeRetrievalReport:
    """Results of one ``RuntimeRetrievalService.retrieve`` call.

    Args:
        request_id: Identifies this retrieval in logs and diagnostics.
        lane: Lane that ranked the results.
        results: Passages, best first, at most ``top_k``.
        diagnostics: What the retrieval found and dropped.
        evidence: Topology evidence selected around the results.
    """

    request_id: str
    lane: RetrievalLane
    results: tuple[RetrievalResult, ...]
    diagnostics: RetrievalDiagnostics
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)


@dataclass(frozen=True, slots=True)
class RetrievalResources:
    """Stores and model clients that one ``RuntimeRetrievalService`` reads through.

    Args:
        embed_client: Embeds the query for the dense lane.
        vector_repository: Vector store every lane searches.
        active_versions: Resolves each document's active version, so candidates
            from superseded versions are dropped.
        chunk_reader: Reads chunk artifacts for the reader tools.
        sparse_encoder: Encodes the query for the sparse lane.
        graph_repository: Knowledge graph for graph search and observation.
        topology_repository: Topology store behind permission checks, topology
            retrieval and the reader tools.
        contextual_search: Contextual evidence search used by topology retrieval.
        document_snapshots: Document snapshots for the reader tools.
        source_catalog: Source catalog for the reader tools.
        summary_repository: Summary views; absent while summarization is off.
        entity_summary_search: Searches accepted source-entity cards and expands the
            hits back into the evidence chunks behind them; absent leaves plain chunk
            retrieval.
        rerank_client: Rerank model client; absent when the model catalog has no
            ``rerank`` section, and a rerank request is then served in retrieval order.
    """

    embed_client: AsyncHarborEmbedClientProtocol
    vector_repository: VectorRepositoryPort
    active_versions: ActiveVersionResolver
    chunk_reader: ChunkArtifactReaderPort
    sparse_encoder: SparseQueryEncoder
    graph_repository: KnowledgeGraphReader | None = None
    topology_repository: TopologySearchPort | None = None
    contextual_search: ContextualSearchPort | None = None
    document_snapshots: DocumentSnapshotReader | None = None
    source_catalog: SourceCatalogReader | None = None
    summary_repository: SummaryReaderPort | None = None
    entity_summary_search: EntitySummarySearch | None = None
    rerank_client: AsyncHarborRerankClientProtocol | None = None


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """Process-wide settings every retrieval runs with.

    Args:
        embedding_model: Logical embedding model of the catalog that embeds queries.
        embedding_dimensions: Dense vector width; must match the index.
        normalize_embeddings: Normalize query embeddings before searching.
        dense_weight: Weight of the dense lane in hybrid fusion, from 0 to 1.
        semantic_weight: Weight of semantic similarity in topology retrieval, from 0 to 1.
        topology: Topology retrieval and evidence budget policy.
    """

    embedding_model: str
    embedding_dimensions: int
    normalize_embeddings: bool = True
    dense_weight: float = 0.7
    semantic_weight: float = 0.5
    topology: TopologyRetrievalPolicy = field(default_factory=TopologyRetrievalPolicy)

    def __post_init__(self) -> None:
        """Reject a blank model, a non-positive width and weights outside 0 to 1."""
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
    """How one ``RuntimeRetrievalService.retrieve`` call searches.

    Args:
        lane: Dense vectors, sparse terms, or both fused.
        filters: Evidence filter, already parsed from the request.
        observe_graph: Observe the knowledge graph around the results.
        graph_seeds: Extra graph observation seeds; see
            ``RetrievalRequest.graph_seed_node_keys``.
        mode: ``FLAT`` ranks passages only; any other mode also selects topology
            evidence around them.
        rerank: Reorder the passages with the rerank model before keeping ``top_k``.
    """

    lane: RetrievalLane = RetrievalLane.HYBRID
    filters: VectorFilter | None = None
    observe_graph: bool = False
    graph_seeds: tuple[str, ...] = ()
    mode: RetrievalMode = RetrievalMode.FLAT
    rerank: bool = False

    def __post_init__(self) -> None:
        """Coerce ``mode`` to a ``RetrievalMode``."""
        object.__setattr__(self, "mode", RetrievalMode(self.mode))


CloseOperation = Callable[[], Awaitable[None]]
