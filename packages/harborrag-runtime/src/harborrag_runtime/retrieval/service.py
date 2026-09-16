"""Authoritative retrieval service implementation."""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.records import CanonicalMention
from harborrag_core.topology.search import RetrievalMode
from harborrag_engine.retrieval import (
    ActiveVersionCandidateValidator,
    AuthoritativeGraphSearch,
    AuthoritativeProjectionSearch,
    AuthoritativeSearchRequest,
    RetrievalLane,
)

from ..contracts import (
    EntityResolveResponse,
    EvidenceFetchResponse,
    RelationSearchResponse,
    SemanticPathRequest,
    SemanticPathResponse,
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
from .evidence import budget_diagnostics, build_evidence_bundle, select_evidence
from .graph_observation import GraphObservation, GraphObserver
from .graph_service import RuntimeGraphRetrievalMixin
from .knowledge import KnowledgeRetrieval
from .permissions import RetrievalPermissions
from .reader_service import RuntimeReaderRetrievalMixin
from .readers import ReaderResources, ReaderRetrieval
from .result_loader import EvidenceResultLoader
from .topology import TopologyRetrieval
from .validation import validate_retrieval_request

logger = logging.getLogger("harborrag.runtime.retrieval")

if TYPE_CHECKING:
    from ..config.settings import RuntimeSettings


class _NullRetrievalTelemetry:
    def record_stale_candidate_rejections(self, count: int) -> None:
        del count


class RuntimeRetrievalService(RuntimeGraphRetrievalMixin, RuntimeReaderRetrievalMixin):
    """Resolve projection visibility through Postgres before loading evidence."""

    def __init__(
        self,
        *,
        resources: RetrievalResources,
        policy: RetrievalPolicy,
        close_resources: tuple[CloseOperation, ...] = (),
        telemetry: RetrievalTelemetry | None = None,
    ) -> None:
        self._sparse = resources.sparse_encoder
        self._graph = resources.graph_repository
        self._summaries = resources.summary_repository
        self._policy = policy
        self._result_loader = EvidenceResultLoader(resources.embed_client, policy)
        self._permissions = RetrievalPermissions(resources.topology_repository)
        self._candidate_validator = ActiveVersionCandidateValidator(resources.active_versions)
        self._knowledge = KnowledgeRetrieval(
            resources.topology_repository,
            resources.vector_repository,
            self._candidate_validator,
            self._permissions,
        )
        self._reader = ReaderRetrieval(
            ReaderResources(
                vectors=resources.vector_repository,
                validator=self._candidate_validator,
                permissions=self._permissions,
                topology=resources.topology_repository,
                snapshots=resources.document_snapshots,
                chunks=resources.chunk_reader,
                sources=resources.source_catalog,
                graph=resources.graph_repository,
                summaries=resources.summary_repository,
            )
        )
        self._topology = TopologyRetrieval(
            resources.topology_repository,
            resources.vector_repository,
            self._candidate_validator,
            policy=policy.topology.model_copy(update={"semantic_weight": policy.semantic_weight}),
            contextual=resources.contextual_search,
        )
        self._search = AuthoritativeProjectionSearch(
            resources.vector_repository,
            self._candidate_validator,
        )
        self._graph_search = (
            AuthoritativeGraphSearch(
                resources.graph_repository,
                resources.active_versions,
                resources.topology_repository,
            )
            if resources.graph_repository is not None
            else None
        )
        self._close_resources = close_resources
        self._telemetry = telemetry or _NullRetrievalTelemetry()
        self._observer = (
            GraphObserver(resources.graph_repository)
            if resources.graph_repository is not None and resources.topology_repository is None
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
            self._sparse.encode_query(query).vector
            if selected.lane in {RetrievalLane.SPARSE, RetrievalLane.HYBRID}
            else None
        )
        dense_vector = (
            await self._result_loader.dense_vector(query)
            if selected.lane in {RetrievalLane.DENSE, RetrievalLane.HYBRID}
            else None
        )
        search = await self._search.search(
            AuthoritativeSearchRequest(
                lane=selected.lane,
                top_k=top_k,
                dense_vector=dense_vector,
                sparse_vector=sparse_vector,
                filters=await self._permissions.scope_filter(selected.filters, context),
                dense_weight=self._policy.dense_weight,
            ),
            context=context,
        )
        topology = await self._topology.prepare(
            query,
            await self._permissions.validate(search.candidates, context),
            options=selected,
            context=context,
            dense_vector=dense_vector,
        )
        loaded, load_failures = await self._result_loader.load_candidates(
            topology.candidates,
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
        loaded, topology_diagnostics = await self._topology.finalize(
            topology, loaded, context=context
        )
        final_validation = await self._candidate_validator.validate(
            tuple(candidate for candidate, _ in loaded)
        )
        permitted = await self._permissions.validate(final_validation.accepted, context)
        active_candidate_ids = {str(candidate.id) for candidate in permitted}
        results = [
            result for candidate, result in loaded if str(candidate.id) in active_candidate_ids
        ]
        if selected.mode != RetrievalMode.FLAT:
            results, tokens, excluded = select_evidence(
                results, topology.flat, top_k=top_k, policy=self._policy.topology
            )
            topology_diagnostics = budget_diagnostics(topology_diagnostics, tokens, excluded)
        results = results[:top_k]
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
            evidence=build_evidence_bundle(tuple(results), topology.evidence, topology_diagnostics),
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
                short_by=max(0, top_k - len(results)),
                topology=topology_diagnostics,
            ),
        )

    async def fetch_evidence(
        self, chunk_ids: tuple[str, ...], *, access: AccessContext
    ) -> EvidenceFetchResponse:
        return await self._knowledge.fetch(chunk_ids, access=access)

    async def resolve_entities(
        self, name: str, *, limit: int, access: AccessContext
    ) -> EntityResolveResponse:
        return await self._knowledge.resolve_entities(name, limit=limit, access=access)

    async def lookup_entities(
        self,
        *,
        entity_ids: tuple[str, ...] = (),
        chunk_ids: tuple[str, ...] = (),
        access: AccessContext,
    ) -> tuple[CanonicalMention, ...]:
        return await self._knowledge.lookup_entities(
            entity_ids=entity_ids,
            chunk_ids=chunk_ids,
            access=access,
        )

    async def find_semantic_relations(
        self,
        entity_id: str,
        *,
        predicates: tuple[str, ...],
        direction: str,
        limit: int,
        access: AccessContext,
    ) -> RelationSearchResponse:
        return await self._knowledge.find_relations(
            entity_id,
            predicates=predicates,
            direction=direction,
            limit=limit,
            access=access,
        )

    async def find_semantic_paths(
        self,
        request: SemanticPathRequest,
    ) -> SemanticPathResponse:
        return await self._knowledge.find_paths(request)

    @staticmethod
    def _retrieval_context(
        *,
        request_id: str,
        tenant_id: str,
        access: AccessContext | None,
    ) -> StorageOperationContext:
        if access is not None:
            if str(access.tenant_id) != tenant_id:
                raise ValueError("retrieval access tenant must match tenant_id")
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
