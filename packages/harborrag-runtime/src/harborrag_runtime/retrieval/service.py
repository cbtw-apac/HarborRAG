"""Authoritative retrieval service implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from time import perf_counter
from uuid import uuid4

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.ingestion import (
    ContentReference,
    DocumentIdentityBuilder,
)
from harborrag_core.models.embed import EmbeddingPurpose, HarborEmbedRequest
from harborrag_core.retrieval import GraphPathQuery, GraphSubgraphQuery, GraphTripletQuery
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval import (
    ActiveVersionCandidateValidator,
    AuthoritativeGraphSearch,
    AuthoritativePathResult,
    AuthoritativeProjectionSearch,
    AuthoritativeSearchRequest,
    AuthoritativeSubgraphResult,
    AuthoritativeTripletResult,
    RetrievalLane,
)

from ..config.settings import RuntimeSettings
from ..ingestion.observability import IngestionTelemetry
from .contracts import (
    CloseOperation,
    RetrievalDiagnostics,
    RetrievalOptions,
    RetrievalPolicy,
    RetrievalResources,
    RuntimeRetrievalReport,
)
from .graph_observation import GraphObservation, GraphObserver
from .validation import required_mapping, required_text, validate_retrieval_request

_CHUNK_LOAD_CONCURRENCY = 8

logger = logging.getLogger("harborrag.runtime.retrieval")


class RuntimeRetrievalService:
    """Resolve projection visibility through Postgres before loading evidence."""

    def __init__(
        self,
        *,
        resources: RetrievalResources,
        policy: RetrievalPolicy,
        close_resources: tuple[CloseOperation, ...] = (),
        telemetry: IngestionTelemetry | None = None,
    ) -> None:
        self._embed = resources.embed_client
        self._chunks = resources.chunk_reader
        self._sparse = resources.sparse_encoder
        self._graph = resources.graph_repository
        self._policy = policy
        self._search = AuthoritativeProjectionSearch(
            resources.vector_repository,
            ActiveVersionCandidateValidator(resources.active_versions),
        )
        self._graph_search = (
            AuthoritativeGraphSearch(resources.graph_repository, resources.active_versions)
            if resources.graph_repository is not None
            else None
        )
        self._close_resources = close_resources
        self._telemetry = telemetry or IngestionTelemetry()
        self._identities = DocumentIdentityBuilder()
        self._observer = (
            GraphObserver(resources.graph_repository, self._identities)
            if resources.graph_repository is not None
            else None
        )
        self._closed = False

    @classmethod
    async def connect(cls, settings: RuntimeSettings) -> RuntimeRetrievalService:
        from ..retrieval_factory import connect_retrieval_service

        return await connect_retrieval_service(settings)

    async def retrieve(
        self,
        query: str,
        *,
        tenant_id: str,
        top_k: int = 10,
        options: RetrievalOptions | None = None,
        access: AccessContext | None = None,
    ) -> RuntimeRetrievalReport:
        """Search projections, reject stale versions, and range-load full chunks."""

        validate_retrieval_request(query, tenant_id, top_k)
        selected = options or RetrievalOptions()
        started = perf_counter()
        request_id = f"retrieval-{uuid4().hex}"
        context = self._retrieval_context(
            request_id=request_id,
            tenant_id=tenant_id,
            access=access,
        )
        sparse_vector = (
            self._sparse.encode(query).vector
            if selected.lane in {RetrievalLane.SPARSE, RetrievalLane.HYBRID}
            else None
        )
        dense_vector = (
            await self._dense_vector(query)
            if selected.lane in {RetrievalLane.DENSE, RetrievalLane.HYBRID}
            else None
        )
        search = await self._search.search(
            AuthoritativeSearchRequest(
                lane=selected.lane,
                top_k=top_k,
                dense_vector=dense_vector,
                sparse_vector=sparse_vector,
                filters=selected.filters,
                dense_weight=self._policy.dense_weight,
            ),
            context=context,
        )
        results, load_failures = await self._load_candidates(
            search.candidates,
            context=context,
            request_id=request_id,
        )
        observation = (
            await self._observer.observe(
                search.candidates,
                context=context,
                request_id=request_id,
            )
            if selected.observe_graph and self._observer is not None
            else GraphObservation()
        )
        duration_ms = (perf_counter() - started) * 1_000
        diagnostics = search.diagnostics
        self._telemetry.record_stale_candidate_rejections(diagnostics.stale_count)
        logger.info(
            "Completed authoritative retrieval",
            extra={
                "request_id": request_id,
                "tenant_id": tenant_id,
                "lane": selected.lane.value,
                "candidate_hits": len(search.candidates),
                "stale_candidates": diagnostics.stale_count,
                "result_count": len(results),
                "duration_ms": duration_ms,
            },
        )
        return RuntimeRetrievalReport(
            request_id=request_id,
            lane=selected.lane,
            results=tuple(results),
            diagnostics=RetrievalDiagnostics(
                candidate_hits=len(search.candidates),
                stale_candidates=diagnostics.stale_count,
                unpublished_candidates=diagnostics.unpublished_count,
                malformed_candidates=diagnostics.malformed_count + load_failures,
                search_window=diagnostics.search_window,
                graph_nodes=observation.nodes,
                graph_relations=observation.relations,
                graph_truncated=observation.truncated,
                duration_ms=duration_ms,
            ),
        )

    async def _load_candidates(
        self,
        candidates: Sequence[VectorSearchResult],
        *,
        context: StorageOperationContext,
        request_id: str,
    ) -> tuple[list[RetrievalResult], int]:
        """Range-load chunks concurrently, skipping candidates that cannot be read."""

        load_limit = asyncio.Semaphore(_CHUNK_LOAD_CONCURRENCY)

        async def load(candidate: VectorSearchResult) -> RetrievalResult:
            async with load_limit:
                return await self._load_result(candidate, context=context)

        loaded = await asyncio.gather(
            *(load(candidate) for candidate in candidates),
            return_exceptions=True,
        )
        results: list[RetrievalResult] = []
        failures = 0
        for candidate, result in zip(candidates, loaded, strict=True):
            if isinstance(result, Exception):
                failures += 1
                logger.warning(
                    "Skipping malformed or unreadable retrieval candidate",
                    extra={"request_id": request_id, "candidate_id": str(candidate.id)},
                    exc_info=(type(result), result, result.__traceback__),
                )
                continue
            if isinstance(result, BaseException):
                raise result
            results.append(result)
        return results, failures

    async def search_graph_triplets(
        self,
        query: GraphTripletQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativeTripletResult:
        return await self._require_graph_search().triplets(
            query,
            context=self._graph_context(access, "graph-triplet-search"),
        )

    async def search_graph_paths(
        self,
        query: GraphPathQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativePathResult:
        return await self._require_graph_search().paths(
            query,
            context=self._graph_context(access, "graph-path-search"),
        )

    async def search_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativeSubgraphResult:
        return await self._require_graph_search().subgraph(
            query,
            context=self._graph_context(access, "graph-subgraph-search"),
        )

    def _require_graph_search(self) -> AuthoritativeGraphSearch:
        if self._graph_search is None:
            raise HarborCapabilityError("graph retrieval is not configured")
        return self._graph_search

    @staticmethod
    def _retrieval_context(
        *,
        request_id: str,
        tenant_id: str,
        access: AccessContext | None,
    ) -> StorageOperationContext:
        if access is not None:
            return StorageOperationContext.for_access(
                access,
                operation_kind="retrieval",
                idempotency_key=request_id,
            )
        return StorageOperationContext.system(
            tenant_id,
            operation_kind="retrieval",
            idempotency_key=request_id,
        )

    @staticmethod
    def _graph_context(
        access: AccessContext,
        operation_kind: str,
    ) -> StorageOperationContext:
        return StorageOperationContext.for_access(
            access,
            operation_kind=operation_kind,
            idempotency_key=f"graph-{uuid4().hex}",
        )

    async def _dense_vector(self, query: str) -> tuple[float, ...]:
        response = await self._embed.aembed(
            request=HarborEmbedRequest(
                inputs=(query,),
                logical_model=self._policy.embedding_model,
                dimensions=self._policy.embedding_dimensions,
                purpose=EmbeddingPurpose.QUERY,
                normalize=self._policy.normalize_embeddings,
                cacheable=False,
                sensitive=True,
            )
        )
        value = response.embeddings[0].value
        if not isinstance(value, tuple):
            raise ValueError("retrieval requires a float query embedding")
        if len(value) != self._policy.embedding_dimensions:
            raise ValueError("retrieval embedding has an unexpected dimension")
        return value

    async def _load_result(
        self,
        candidate: VectorSearchResult,
        *,
        context: StorageOperationContext,
    ) -> RetrievalResult:
        payload = candidate.payload
        reference = ContentReference.model_validate(required_mapping(payload, "content_reference"))
        chunk = await self._chunks.get_reference(reference, context=context)
        chunk_id = required_text(payload, "chunk_id")
        if str(chunk.chunk_id) != chunk_id:
            raise ValueError("Qdrant content reference resolved to another chunk")
        return RetrievalResult(
            id=chunk_id,
            text=chunk.content,
            score=candidate.score,
            metadata={
                "document_id": required_text(payload, "document_id"),
                "document_version_id": required_text(payload, "document_version_id"),
                "record_kind": required_text(payload, "record_kind"),
                "chunk_kind": required_text(payload, "chunk_kind"),
                "connector_type": required_text(payload, "connector_type"),
                "citation_locator": payload.get("citation_locator", {}),
                "quality_score": payload.get("quality_score"),
                "retrieval_source": "qdrant-authoritative",
            },
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        results = await asyncio.gather(
            *(close() for close in reversed(self._close_resources)),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        fatal = [
            result
            for result in results
            if isinstance(result, BaseException) and not isinstance(result, Exception)
        ]
        if fatal:
            raise BaseExceptionGroup("retrieval resource close failed", fatal)
        if errors:
            # Deliberately left un-closed so a caller can retry aclose() and give the
            # failed resources another attempt; close operations must be idempotent.
            raise ExceptionGroup("retrieval resource close failed", errors)
        self._closed = True
