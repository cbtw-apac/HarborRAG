"""Authoritative retrieval service implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.models.embed import EmbeddingPurpose, HarborEmbedRequest
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval import (
    ActiveVersionCandidateValidator,
    AuthoritativeGraphSearch,
    AuthoritativeProjectionSearch,
    AuthoritativeSearchRequest,
    RetrievalLane,
)

from .contracts import (
    CloseOperation,
    RetrievalDiagnostics,
    RetrievalOptions,
    RetrievalPolicy,
    RetrievalResources,
    RetrievalTelemetry,
    RuntimeRetrievalReport,
)
from .graph_observation import GraphObservation, GraphObserver
from .graph_service import RuntimeGraphRetrievalMixin
from .validation import required_text, validate_retrieval_request

_CHUNK_LOAD_CONCURRENCY = 8

logger = logging.getLogger("harborrag.runtime.retrieval")

if TYPE_CHECKING:
    from ..config.settings import RuntimeSettings


class _NullRetrievalTelemetry:
    def record_stale_candidate_rejections(self, count: int) -> None:
        del count


class RuntimeRetrievalService(RuntimeGraphRetrievalMixin):
    """Resolve projection visibility through Postgres before loading evidence."""

    def __init__(
        self,
        *,
        resources: RetrievalResources,
        policy: RetrievalPolicy,
        close_resources: tuple[CloseOperation, ...] = (),
        telemetry: RetrievalTelemetry | None = None,
    ) -> None:
        self._embed = resources.embed_client
        self._sparse = resources.sparse_encoder
        self._graph = resources.graph_repository
        self._policy = policy
        self._candidate_validator = ActiveVersionCandidateValidator(resources.active_versions)
        self._search = AuthoritativeProjectionSearch(
            resources.vector_repository,
            self._candidate_validator,
        )
        self._graph_search = (
            AuthoritativeGraphSearch(resources.graph_repository, resources.active_versions)
            if resources.graph_repository is not None
            else None
        )
        self._close_resources = close_resources
        self._telemetry = telemetry or _NullRetrievalTelemetry()
        self._observer = (
            GraphObserver(resources.graph_repository)
            if resources.graph_repository is not None
            else None
        )
        self._closed = False

    @classmethod
    async def connect(cls, settings: RuntimeSettings) -> RuntimeRetrievalService:
        from .composition import connect_retrieval_service

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
        """Search active evidence vectors and return their canonical payload content."""

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
        loaded, load_failures = await self._load_candidates(
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
        final_validation = await self._candidate_validator.validate(
            tuple(candidate for candidate, _ in loaded)
        )
        active_candidate_ids = {str(candidate.id) for candidate in final_validation.accepted}
        results = [
            result for candidate, result in loaded if str(candidate.id) in active_candidate_ids
        ]
        if final_validation.rejected_count:
            # Graph observation is optional diagnostic context. Discard it when
            # publication advanced during retrieval so it cannot describe a
            # candidate that the final authority check removed.
            observation = GraphObservation()
        duration_ms = (perf_counter() - started) * 1_000
        diagnostics = search.diagnostics
        stale_count = diagnostics.stale_count + final_validation.stale_count
        unpublished_count = diagnostics.unpublished_count + final_validation.unpublished_count
        malformed_count = (
            diagnostics.malformed_count + final_validation.malformed_count + load_failures
        )
        self._telemetry.record_stale_candidate_rejections(stale_count)
        logger.info(
            "Completed authoritative retrieval",
            extra={
                "request_id": request_id,
                "tenant_id": tenant_id,
                "lane": selected.lane.value,
                "candidate_hits": len(search.candidates),
                "stale_candidates": stale_count,
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
                stale_candidates=stale_count,
                unpublished_candidates=unpublished_count,
                malformed_candidates=malformed_count,
                search_window=diagnostics.search_window,
                graph_nodes=observation.nodes,
                graph_relations=observation.relations,
                graph_truncated=observation.truncated,
                duration_ms=duration_ms,
                graph_documents=observation.documents,
            ),
        )

    async def _load_candidates(
        self,
        candidates: Sequence[VectorSearchResult],
        *,
        context: StorageOperationContext,
        request_id: str,
    ) -> tuple[list[tuple[VectorSearchResult, RetrievalResult]], int]:
        """Validate candidates concurrently, skipping malformed payloads."""

        load_limit = asyncio.Semaphore(_CHUNK_LOAD_CONCURRENCY)

        async def load(candidate: VectorSearchResult) -> RetrievalResult:
            async with load_limit:
                return await self._load_result(candidate, context=context)

        loaded = await asyncio.gather(
            *(load(candidate) for candidate in candidates),
            return_exceptions=True,
        )
        results: list[tuple[VectorSearchResult, RetrievalResult]] = []
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
            results.append((candidate, result))
        return results, failures

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
        del context
        chunk_id = required_text(payload, "chunk_id")
        return RetrievalResult(
            id=chunk_id,
            text=required_text(payload, "content"),
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
