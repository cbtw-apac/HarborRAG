"""Authoritative retrieval service implementation."""

from __future__ import annotations

import logging
from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from harborrag_adapters.repositories.errors import HarborStorageNotFoundError
from harborrag_core.contracts.reader import (
    EntityFindRequest,
    EntityFindResponse,
    EntityResolveResponse,
    EvidenceFetchResponse,
    RelationSearchResponse,
    SemanticPathRequest,
    SemanticPathResponse,
)
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.records import CanonicalMention
from harborrag_core.topology.search import RetrievalMode
from harborrag_engine.ingestion.projections.vector import EVIDENCE_INDEX
from harborrag_engine.retrieval import (
    ActiveVersionCandidateValidator,
    AuthoritativeGraphSearch,
    AuthoritativeProjectionSearch,
    AuthoritativeSearchDiagnostics,
    AuthoritativeSearchRequest,
    AuthoritativeSearchResult,
    RetrievalLane,
)
from harborrag_engine.retrieval.duplicates import collapse_duplicates
from harborrag_engine.retrieval.evidence_filters import validate_evidence_filter

from .contracts import (
    CloseOperation,
    RetrievalDiagnostics,
    RetrievalOptions,
    RetrievalPolicy,
    RetrievalResources,
    RetrievalTelemetry,
    RuntimeRetrievalReport,
)
from .entity_summary import split_facet_filters
from .errors import no_entity_index, no_indexed_content
from .evidence import budget_diagnostics, build_evidence_bundle, select_evidence
from .graph_observation import GraphObservation, GraphObserver
from .graph_service import RuntimeGraphRetrievalMixin
from .knowledge import KnowledgeRetrieval
from .permissions import RetrievalPermissions
from .reader_service import RuntimeReaderRetrievalMixin
from .readers import ReaderResources, ReaderRetrieval
from .result_loader import EvidenceResultLoader
from .source_fields import SourceFieldTrace, split_field_filters
from .topology import TopologyRetrieval
from .validation import validate_retrieval_request

_NO_CANDIDATES = AuthoritativeSearchDiagnostics(
    candidate_count=0,
    accepted_count=0,
    stale_count=0,
    unpublished_count=0,
    malformed_count=0,
    search_window=0,
    exhausted=True,
)

logger = logging.getLogger("harborrag.runtime.retrieval")

if TYPE_CHECKING:
    from harborrag_core.ports.storage import VectorRepositoryPort

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
        self._entities = resources.entity_summary_search
        self._policy = policy
        self._result_loader = EvidenceResultLoader(resources.embed_client, policy)
        self._permissions = RetrievalPermissions(resources.topology_repository)
        self._candidate_validator = ActiveVersionCandidateValidator(resources.active_versions)
        # Retained so the conversation-memory index can share this one connected
        # Qdrant client instead of opening a second one; memory lives in its own
        # logical collection, never the document/evidence one.
        self._vector_repository = resources.vector_repository
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
        self._fields = SourceFieldTrace(resources.vector_repository, EVIDENCE_INDEX)
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

    async def find_entities(self, request: EntityFindRequest) -> EntityFindResponse:
        """Rank source entities for a question, within the facet values a caller names.

        The entity index answers "which entities, in what order"; the summary
        authority answers "which of those may this principal see, and with what
        evidence". Nothing from the index is returned unless the authority
        released it, so an entity whose binding is stale or private is simply
        absent, not shown without its evidence. A missing index, and (on a shared
        corpus) hits nothing was released for, come back as reason codes rather
        than as an empty answer that looks complete -- see ``EntitySummarySearch.find``.
        """

        request_id = f"entities-{uuid4().hex}"
        if self._entities is None or self._summaries is None:
            raise no_entity_index()
        context = self._retrieval_context(
            request_id=request_id,
            tenant_id=str(request.access.tenant_id),
            access=request.access,
        )
        dense_vector = await self._result_loader.dense_vector(request.query)
        return await self._entities.find(request, request_id, dense_vector, context)

    @property
    def vector_repository(self) -> VectorRepositoryPort:
        """The connected vector client, for sharing with the memory index."""

        return self._vector_repository

    @property
    def embedding_dimensions(self) -> int:
        """Dense vector width, so a second index matches this deployment."""

        return self._policy.embedding_dimensions

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
        # Before permission conditions join it: only the caller's own keys are
        # checked, and an unindexed one is refused rather than scanned.
        validate_evidence_filter(selected.filters)
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
        # A ``facet.*`` filter addresses entity summary points, not chunks. It is
        # applied on the entity lane and taken out of the chunk filter; left in, it
        # would match nothing on chunks and force the flat lane for no reason.
        facet_filter, chunk_filter = (
            split_facet_filters(selected.filters)
            if self._entities is not None
            else (None, selected.filters)
        )
        if facet_filter is not None:
            selected = replace(selected, filters=chunk_filter)
        # A ``fields.*`` filter names an item's own fields -- a Jira issue's custom
        # fields -- which its attachments do not carry. It is resolved to the
        # matching items first, then applied as "their evidence or evidence
        # attached to them", so a CV is reached through the issue it belongs to.
        field_filter, chunk_filter = split_field_filters(chunk_filter)
        traced: tuple[str, ...] | None = None
        try:
            if field_filter is not None:
                traced = await self._fields.matching_items(
                    await self._permissions.scope_filter(field_filter, context),
                    context=context,
                )
                chunk_filter = SourceFieldTrace.scope(traced, chunk_filter)
                selected = replace(selected, filters=chunk_filter)
            search = (
                await self._search.search(
                    AuthoritativeSearchRequest(
                        lane=selected.lane,
                        top_k=top_k,
                        dense_vector=dense_vector,
                        sparse_vector=sparse_vector,
                        filters=await self._permissions.scope_filter(chunk_filter, context),
                        dense_weight=self._policy.dense_weight,
                    ),
                    context=context,
                )
                if traced != ()
                # No item has those field values: nothing can qualify, and an empty
                # membership condition is not one every backend accepts.
                else AuthoritativeSearchResult((), _NO_CANDIDATES)
            )
        except HarborStorageNotFoundError as exc:
            logger.info(
                "Retrieval found no index for the tenant",
                extra={"request_id": request_id, "tenant_id": tenant_id},
            )
            raise no_indexed_content() from exc
        candidates = search.candidates
        # Enrichment fuses entity evidence in without the chunk filter, which would
        # readmit evidence of items the fields filter just excluded.
        if self._entities is not None and (facet_filter is not None or field_filter is None):
            # Entity-reached chunks join the authoritative set before validation,
            # so they face exactly the permission and active-version checks every
            # other candidate faces -- reaching them differently is not a reason
            # to serve them differently. With a facet filter the lane selects
            # instead of enriching: only qualifying entities' evidence remains.
            candidates = await self._entities.expand(
                candidates, dense_vector, context=context, facet_filter=facet_filter
            )
        topology = await self._topology.prepare(
            query,
            await self._permissions.validate(candidates, context),
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
                memory_seeds=selected.graph_seeds,
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
        # Entity- and topology-reached chunks join after the search collapsed its
        # own duplicates, so the same text can arrive twice by different routes.
        distinct, late_duplicates = collapse_duplicates(
            [result for candidate, result in loaded if str(candidate.id) in active_candidate_ids],
            lambda result: result.metadata.get("content_hash"),
            limit=top_k,
        )
        results = list(distinct)
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
                duplicates_collapsed=diagnostics.collapsed_count + late_duplicates,
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
        results: list[BaseException] = []
        for close in reversed(self._close_resources):
            try:
                await close()
            except BaseException as error:
                results.append(error)
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
