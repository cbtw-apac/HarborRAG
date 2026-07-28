"""Production semantic retrieval with bounded graph expansion."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from uuid import uuid4

from harborrag_adapters.repositories.graph.base import HarborGraphRepository
from harborrag_adapters.repositories.vector.base import HarborVectorRepository
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.embed import (
    EmbeddingPurpose,
    HarborEmbedRequest,
)
from harborrag_core.ports.model_clients import AsyncHarborEmbedClientProtocol
from harborrag_core.schemas.graph import (
    GraphExpansionQuery,
    GraphNode,
    GraphSubgraph,
)
from harborrag_core.schemas.ids import EntityId, TenantId
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import (
    FilterOperator,
    VectorFilter,
    VectorFilterCondition,
    VectorSearchQuery,
)
from harborrag_engine.ingestion.chunking.manifest import CanonicalChunkRepository
from harborrag_engine.ingestion.indexing.config import IndexingConfig
from harborrag_engine.ingestion.indexing.graph.projectionstate import (
    deterministic_graph_node_id,
)
from harborrag_engine.retrieval.fusion import reciprocal_rank_fusion

from .config.settings import RuntimeSettings

_MAX_QUERY_CHARACTERS = 32_000
_VECTOR_OVERSAMPLE = 3
_GRAPH_EXPANSION_DEPTH = 2
_GRAPH_FUSION_WEIGHT = 0.5

logger = logging.getLogger("harborrag.runtime.retrieval")


@dataclass(frozen=True, slots=True)
class RetrievalDiagnostics:
    vector_hits: int
    graph_nodes: int
    graph_edges: int
    graph_hits: int
    graph_truncated: bool
    duration_ms: float


@dataclass(frozen=True, slots=True)
class RuntimeRetrievalReport:
    request_id: str
    results: tuple[RetrievalResult, ...]
    diagnostics: RetrievalDiagnostics


@dataclass(frozen=True, slots=True)
class RetrievalResources:
    embed_client: AsyncHarborEmbedClientProtocol
    vector_repository: HarborVectorRepository
    graph_repository: HarborGraphRepository
    chunk_repository: CanonicalChunkRepository


class RuntimeRetrievalService:
    """Own retrieval providers and keep tenant scope on every repository call."""

    def __init__(
        self,
        *,
        resources: RetrievalResources,
        indexing_config: IndexingConfig,
        close_resources: tuple[Callable[[], Awaitable[None]], ...] = (),
    ) -> None:
        self._embed = resources.embed_client
        self._vectors = resources.vector_repository
        self._graph = resources.graph_repository
        self._chunks = resources.chunk_repository
        self._config = indexing_config
        self._close_resources = close_resources
        self._closed = False

    @classmethod
    async def connect(cls, settings: RuntimeSettings) -> RuntimeRetrievalService:
        """Build and connect the configured retrieval dependencies."""

        from .retrieval_factory import connect_retrieval_service

        return await connect_retrieval_service(settings)

    async def retrieve(
        self,
        query: str,
        *,
        tenant_id: str,
        top_k: int = 10,
    ) -> RuntimeRetrievalReport:
        """Retrieve canonical chunks and enrich their ranking through FalkorDB."""

        self._validate(query, tenant_id, top_k)
        started = perf_counter()
        request_id = f"retrieval-{uuid4().hex}"
        context = StorageOperationContext(
            tenant_id=TenantId(tenant_id),
            request_id=request_id,
            retrieval_request_id=request_id,
        )
        vector_hits = await self._semantic_search(
            query,
            top_k=top_k * _VECTOR_OVERSAMPLE,
            context=context,
        )
        graph_hits, node_count, edge_count, truncated = await self._graph_expand(
            vector_hits[:top_k],
            top_k=top_k * _VECTOR_OVERSAMPLE,
            context=context,
        )
        rankings = (graph_hits, vector_hits) if graph_hits else (vector_hits,)
        weights = (_GRAPH_FUSION_WEIGHT, 1.0) if graph_hits else (1.0,)
        fused = tuple(reciprocal_rank_fusion(rankings, weights=weights)[:top_k])
        duration_ms = (perf_counter() - started) * 1_000
        logger.info(
            "Completed hybrid retrieval",
            extra={
                "request_id": request_id,
                "tenant_id": tenant_id,
                "vector_hits": len(vector_hits),
                "graph_hits": len(graph_hits),
                "result_count": len(fused),
                "duration_ms": duration_ms,
            },
        )
        return RuntimeRetrievalReport(
            request_id=request_id,
            results=fused,
            diagnostics=RetrievalDiagnostics(
                vector_hits=len(vector_hits),
                graph_nodes=node_count,
                graph_edges=edge_count,
                graph_hits=len(graph_hits),
                graph_truncated=truncated,
                duration_ms=duration_ms,
            ),
        )

    async def _semantic_search(
        self,
        text: str,
        *,
        top_k: int,
        context: StorageOperationContext,
    ) -> tuple[RetrievalResult, ...]:
        response = await self._embed.aembed(
            request=HarborEmbedRequest(
                inputs=(text,),
                logical_model=self._config.embedding_model,
                dimensions=self._config.embedding_dimensions,
                purpose=EmbeddingPurpose.QUERY,
                normalize=self._config.normalize_embeddings,
                cacheable=False,
                sensitive=True,
            )
        )
        value = response.embeddings[0].value
        if not isinstance(value, tuple):
            raise ValueError("retrieval requires a float query embedding")
        matches = await self._vectors.search(
            VectorSearchQuery(
                collection=self._config.vector_collection,
                vector=list(value),
                top_k=top_k,
                filters=VectorFilter(
                    must=[
                        VectorFilterCondition(
                            field="is_active",
                            operator=FilterOperator.EQUALS,
                            value=True,
                        )
                    ]
                ),
            ),
            context=context,
        )
        revisions = tuple(
            self._required_text(item.payload, "chunk_revision_id") for item in matches
        )
        records = await self._chunks.get_many(str(context.tenant_id), revisions)
        by_revision = {str(record.chunk_revision_id): record for record in records}
        return tuple(
            RetrievalResult(
                id=revision,
                text=by_revision[revision].content,
                score=match.score,
                metadata={
                    "artifact_id": self._required_text(match.payload, "artifact_id"),
                    "generation_id": self._required_text(match.payload, "generation_id"),
                    "chunk_revision_id": revision,
                    "source_kind": match.payload.get("source_kind", "unknown"),
                    "chunk_role": match.payload.get("chunk_role", "body"),
                    "retrieval_source": "qdrant",
                },
            )
            for match, revision in zip(matches, revisions, strict=True)
        )

    async def _graph_expand(
        self,
        seeds: tuple[RetrievalResult, ...],
        *,
        top_k: int,
        context: StorageOperationContext,
    ) -> tuple[tuple[RetrievalResult, ...], int, int, bool]:
        if not seeds:
            return (), 0, 0, False
        start_nodes = [
            EntityId(
                deterministic_graph_node_id(
                    namespace=self._config.graph_namespace,
                    tenant_id=str(context.tenant_id),
                    generation_id=self._required_text(seed.metadata, "generation_id"),
                    artifact_id=self._required_text(seed.metadata, "artifact_id"),
                    kind="chunk",
                    key=self._required_text(seed.metadata, "chunk_revision_id"),
                )
            )
            for seed in seeds
        ]
        subgraph = await self._graph.expand(
            GraphExpansionQuery(
                start_nodes=start_nodes,
                max_depth=_GRAPH_EXPANSION_DEPTH,
                max_nodes=min(5_000, max(20, top_k * 4)),
            ),
            context=context,
        )
        candidates = self._rank_graph_nodes(subgraph, start_nodes)
        revisions = tuple(
            dict.fromkeys(
                self._required_text(node.properties, "chunk_revision_id") for node in candidates
            )
        )[:top_k]
        records = await self._chunks.get_many(str(context.tenant_id), revisions)
        by_revision = {str(record.chunk_revision_id): record for record in records}
        results = tuple(
            RetrievalResult(
                id=revision,
                text=by_revision[revision].content,
                score=1.0,
                metadata={"retrieval_source": "falkordb"},
            )
            for revision in revisions
        )
        return results, len(subgraph.nodes), len(subgraph.edges), subgraph.truncated

    @staticmethod
    def _rank_graph_nodes(
        subgraph: GraphSubgraph,
        start_nodes: list[EntityId],
    ) -> tuple[GraphNode, ...]:
        """Rank active chunks by semantic seed priority and graph distance."""

        adjacency: dict[str, set[str]] = {}
        for edge in subgraph.edges:
            source_id = str(edge.source_id)
            target_id = str(edge.target_id)
            adjacency.setdefault(source_id, set()).add(target_id)
            adjacency.setdefault(target_id, set()).add(source_id)

        fallback = len(start_nodes) + _GRAPH_EXPANSION_DEPTH + 1
        priorities: dict[str, tuple[int, int, int, str]] = {}
        for seed_rank, seed in enumerate(start_nodes, start=1):
            seed_id = str(seed)
            queue = deque([(seed_id, 0)])
            visited = {seed_id}
            while queue:
                node_id, distance = queue.popleft()
                priority = (
                    seed_rank + distance,
                    distance,
                    seed_rank,
                    node_id,
                )
                if priority < priorities.get(
                    node_id,
                    (fallback, fallback, fallback, node_id),
                ):
                    priorities[node_id] = priority
                if distance >= _GRAPH_EXPANSION_DEPTH:
                    continue
                for neighbor in sorted(adjacency.get(node_id, ())):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append((neighbor, distance + 1))

        candidates = (
            node
            for node in subgraph.nodes
            if "Chunk" in node.labels and node.properties.get("is_active") is True
        )
        return tuple(
            sorted(
                candidates,
                key=lambda node: priorities.get(
                    str(node.id),
                    (fallback, fallback, fallback, str(node.id)),
                ),
            )
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(*(close() for close in reversed(self._close_resources)))

    @staticmethod
    def _required_text(values: object, key: str) -> str:
        if not isinstance(values, dict):
            raise ValueError("retrieval metadata is invalid")
        value = values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"retrieval metadata is missing {key}")
        return value

    @staticmethod
    def _validate(query: str, tenant_id: str, top_k: int) -> None:
        if not query.strip() or len(query) > _MAX_QUERY_CHARACTERS:
            raise ValueError("retrieval query length is invalid")
        if not tenant_id.strip():
            raise ValueError("retrieval tenant must be non-empty")
        if not 1 <= top_k <= 100:
            raise ValueError("retrieval top_k must be between 1 and 100")
