"""Let a question about a whole source entity reach that entity's evidence.

Chunk search answers "find the sentence about Kubernetes". It answers "which
candidates cleared the second round and have banking experience" badly, because
that answer is spread across an issue's body, its comments and an attached CV --
three documents, no single chunk. The source-entity summary is the object such a
question is really about, so it is searched first and then expanded back into the
ordinary evidence chunks that can be cited.

Nothing is served from the summary point itself. It yields a node key; the
summary authority decides, per request, whether that binding is readable and
current, and answers with chunk identities. Those chunks then go through the same
permission validation and loading as every other candidate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from harborrag_core.contracts.reader import (
    ENTITIES_WITHOUT_RELEASED_EVIDENCE,
    ENTITY_INDEX_UNAVAILABLE,
    EntityFindRequest,
    EntityFindResponse,
    EntityMatch,
)
from harborrag_core.indexing import (
    FilterOperator,
    VectorFilter,
    VectorFilterCondition,
    VectorSearchQuery,
    VectorSearchResult,
)
from harborrag_core.ingestion import DocumentIdentityBuilder
from harborrag_core.ports.storage import VectorRepositoryPort
from harborrag_core.ports.summary_projection import SummaryReaderPort
from harborrag_core.storage import StorageOperationContext
from harborrag_core.summaries import SummaryCard
from harborrag_core.topology.derived import (
    ENTITY_SUMMARY_RECORD_KIND,
    ContextualIndexProfile,
)
from harborrag_engine.retrieval.fusion import fuse_candidates

_MAX_ENTITIES = 8
_MAX_CHUNKS_PER_ENTITY = 8
# With facet filters the lane is doing selection, not enrichment: the caller has
# said which entities count, so the ceiling is how many of them we will rank.
_MAX_SELECTED_ENTITIES = 50
FACET_FILTER_PREFIX = "facet."


_RANGE_OPERATORS = {
    "gte": FilterOperator.GREATER_THAN_OR_EQUAL,
    "gt": FilterOperator.GREATER_THAN,
    "lte": FilterOperator.LESS_THAN_OR_EQUAL,
    "lt": FilterOperator.LESS_THAN,
}


def facet_filter_from_mapping(
    facets: Mapping[str, object], source_scope_ids: tuple[str, ...] = ()
) -> VectorFilter | None:
    """Turn ``{"stage": "round 2", "years": {"gte": 5}}`` into entity-point conditions.

    Text facet values are matched case-folded, the way they were stored, so a
    caller's ``Java`` finds a card's ``java``.
    """

    must: list[VectorFilterCondition] = []
    for name, value in sorted(facets.items()):
        field = f"{FACET_FILTER_PREFIX}{name}"
        if isinstance(value, Mapping):
            for bound, raw in value.items():
                operator = _RANGE_OPERATORS.get(str(bound))
                if operator is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise ValueError(f"facet {name!r} range bound {bound!r} is not supported")
                must.append(VectorFilterCondition(field=field, operator=operator, value=raw))
        elif isinstance(value, (list, tuple)):
            must.append(
                VectorFilterCondition(
                    field=field,
                    operator=FilterOperator.IN,
                    value=[_folded(item) for item in value],
                )
            )
        else:
            must.append(VectorFilterCondition(field=field, value=_folded(value)))
    if source_scope_ids:
        must.append(
            VectorFilterCondition(
                field="source_scope_id", operator=FilterOperator.IN, value=list(source_scope_ids)
            )
        )
    return VectorFilter(must=must) if must else None


def _folded(value: object) -> object:
    return value.strip().casefold() if isinstance(value, str) else value


@dataclass(frozen=True)
class EntityHit:
    node_key: str
    source_scope_id: str | None
    score: float
    coverage_mode: str | None


def split_facet_filters(
    filters: VectorFilter | None,
) -> tuple[VectorFilter | None, VectorFilter | None]:
    """Separate ``facet.*`` conditions from the ones meant for evidence payloads.

    The two address different points -- an entity's facets live on its summary
    point, everything else on chunks -- so one filter object has to be routed to
    two searches. Returns ``(facet_filter, remaining_filter)``, each ``None`` when
    it has no conditions.
    """

    if filters is None:
        return None, None
    facet: dict[str, list[VectorFilterCondition]] = {"must": [], "should": [], "must_not": []}
    rest: dict[str, list[VectorFilterCondition]] = {"must": [], "should": [], "must_not": []}
    for clause in facet:
        for condition in getattr(filters, clause):
            target = facet if condition.field.startswith(FACET_FILTER_PREFIX) else rest
            target[clause].append(condition)
    return (
        VectorFilter(**facet) if any(facet.values()) else None,
        VectorFilter(**rest) if any(rest.values()) else None,
    )


@dataclass(frozen=True)
class EntitySummarySearch:
    """Search accepted source-entity cards and return the evidence behind them."""

    summaries: SummaryReaderPort
    vectors: VectorRepositoryPort
    profile: ContextualIndexProfile
    max_entities: int = _MAX_ENTITIES
    max_chunks_per_entity: int = _MAX_CHUNKS_PER_ENTITY
    entity_weight: float = 0.3
    rrf_constant: int = 60

    async def expand(
        self,
        candidates: tuple[VectorSearchResult, ...],
        dense_vector: tuple[float, ...] | None,
        *,
        context: StorageOperationContext,
        facet_filter: VectorFilter | None = None,
    ) -> tuple[VectorSearchResult, ...]:
        """Fuse entity-reached evidence into the candidate set -- or replace it.

        Without facet filters this is enrichment: entity hits join the chunk
        candidates by rank fusion. With them it is selection: the caller has said
        which entities qualify, so only evidence from qualifying entities may be
        returned, and the chunk candidates are set aside.

        Returns the input unchanged on every path that cannot be served safely,
        so a missing index, an empty vector or an unreadable binding degrades to
        plain chunk retrieval instead of failing the request.
        """

        if dense_vector is None or len(dense_vector) != self.profile.dimension:
            return candidates if facet_filter is None else ()
        node_keys = await self.matching_entities(dense_vector, context, facet_filter)
        reached = await self._evidence_for(node_keys, context)
        if facet_filter is not None:
            return reached
        if not reached:
            return candidates
        return fuse_candidates(
            candidates,
            reached,
            semantic_weight=self.entity_weight,
            rrf_constant=self.rrf_constant,
        )

    async def matching_entities(
        self,
        dense_vector: tuple[float, ...],
        context: StorageOperationContext,
        facet_filter: VectorFilter | None = None,
        *,
        limit: int | None = None,
    ) -> tuple[str, ...]:
        """Rank entity node keys by similarity, within the facet filter if any."""

        return tuple(
            hit.node_key
            for hit in await self.ranked_entities(dense_vector, context, facet_filter, limit=limit)
        )

    async def ranked_entities(
        self,
        dense_vector: tuple[float, ...],
        context: StorageOperationContext,
        facet_filter: VectorFilter | None = None,
        *,
        limit: int | None = None,
    ) -> tuple[EntityHit, ...]:
        """Rank entity points by similarity, within the facet filter if any.

        Returns routing data only. Whether any of it may be *shown* is the summary
        authority's call, made per request by the caller that holds the access.
        A missing index ranks nothing here; ``find`` is where that is reported.
        """

        return await self._rank(dense_vector, context, facet_filter, limit=limit) or ()

    async def find(
        self,
        request: EntityFindRequest,
        request_id: str,
        dense_vector: tuple[float, ...],
        context: StorageOperationContext,
    ) -> EntityFindResponse:
        """Answer ``find_entities``: rank entity points, keep what the authority releases.

        Three different situations used to produce the same empty, *complete*
        answer -- no index published for this tenant, nothing matched, and hits
        whose cards the authority would not release -- so a caller could not tell
        "the corpus has no such candidate" from "entity search cannot answer yet;
        use chunk search". They are told apart here with reason codes.

        Released-nothing hits are counted only for a tenant-shared corpus. There
        every principal reads every released card, so a withheld hit can only mean
        a card not yet written or gone stale. Under source ACLs the same count
        would tell a principal how many entities they may not see match their
        facets, so those hits stay silently absent, as the authority intends.
        """

        facet_filter = facet_filter_from_mapping(request.facets, request.source_scope_ids)
        hits = await self._rank(dense_vector, context, facet_filter, limit=request.limit + 1)
        if hits is None:
            return EntityFindResponse(request_id, (), reasons=(ENTITY_INDEX_UNAVAILABLE,))
        if not hits:
            return EntityFindResponse(request_id, ())
        tenant_id = str(request.access.tenant_id)
        keys = tuple(hit.node_key for hit in hits)
        evidence, views = await asyncio.gather(
            self.summaries.entity_evidence(tenant_id, keys, access=request.access),
            self.summaries.views(tenant_id, keys, access=request.access),
        )
        matches: list[EntityMatch] = []
        withheld = 0
        for hit in hits:
            if len(matches) == request.limit:
                break
            chunks = evidence.get(hit.node_key)
            view = views.get(hit.node_key)
            if not chunks or view is None or view.card is None:
                withheld += 1
                continue
            matches.append(
                self._match(hit, view.card, view.coverage_mode, chunks, rank=len(matches) + 1)
            )
        truncated = len(hits) > request.limit
        if not withheld or request.access.corpus_mode != "tenant_shared":
            return EntityFindResponse(request_id, tuple(matches), truncated=truncated)
        return EntityFindResponse(
            request_id,
            tuple(matches),
            truncated=truncated,
            reasons=(ENTITIES_WITHOUT_RELEASED_EVIDENCE,),
            withheld_count=withheld,
        )

    def _match(  # noqa: PLR0913 - the parts of one released match
        self,
        hit: EntityHit,
        card: SummaryCard,
        coverage_mode: str | None,
        chunks: tuple[str, ...],
        *,
        rank: int,
    ) -> EntityMatch:
        return EntityMatch(
            node_key=hit.node_key,
            source_scope_id=hit.source_scope_id,
            rank=rank,
            score=hit.score,
            description=card.description,
            coverage_mode=coverage_mode,
            attributes=tuple(
                {
                    "name": item.name,
                    "values": list(item.values),
                    "from_document_ids": list(item.from_document_ids),
                }
                for item in card.attributes
            ),
            evidence_chunk_ids=chunks[: self.max_chunks_per_entity],
        )

    async def _rank(
        self,
        dense_vector: tuple[float, ...],
        context: StorageOperationContext,
        facet_filter: VectorFilter | None,
        *,
        limit: int | None,
    ) -> tuple[EntityHit, ...] | None:
        """Ranked hits, or ``None`` when this tenant has no entity index at all."""

        index = self.profile.entity_index_name
        if not await self.vectors.index_exists(index, context=context):
            return None
        filters = VectorFilter(
            must=[
                VectorFilterCondition(
                    field="embedding_profile", value=self.profile.entity_fingerprint
                ),
                *(facet_filter.must if facet_filter else []),
            ],
            should=list(facet_filter.should) if facet_filter else [],
            must_not=list(facet_filter.must_not) if facet_filter else [],
        )
        hits = await self.vectors.search(
            VectorSearchQuery(
                index_name=index,
                vector=list(dense_vector),
                top_k=limit
                or (_MAX_SELECTED_ENTITIES if facet_filter is not None else self.max_entities),
                filters=filters,
            ),
            context=context,
        )
        ranked: dict[str, EntityHit] = {}
        for hit in hits:
            payload = hit.payload
            key = payload.get("node_key")
            if (
                payload.get("record_kind") != ENTITY_SUMMARY_RECORD_KIND
                or payload.get("projection_point_id") != hit.id
                or not isinstance(key, str)
                or key in ranked
            ):
                continue
            scope = payload.get("source_scope_id")
            coverage = payload.get("coverage_mode")
            ranked[key] = EntityHit(
                key,
                scope if isinstance(scope, str) else None,
                float(hit.score),
                coverage if isinstance(coverage, str) else None,
            )
        return tuple(ranked.values())

    async def _evidence_for(
        self,
        node_keys: tuple[str, ...],
        context: StorageOperationContext,
    ) -> tuple[VectorSearchResult, ...]:
        if not node_keys:
            return ()
        evidence = await self.summaries.entity_evidence(
            str(context.tenant_id), node_keys, access=context.access
        )
        identity = DocumentIdentityBuilder()
        chunks = tuple(
            dict.fromkeys(
                chunk
                for key in node_keys
                for chunk in evidence.get(key, ())[: self.max_chunks_per_entity]
            )
        )
        if not chunks:
            return ()
        records = await self.vectors.get_records(
            "evidence",
            tuple(identity.point_id(chunk_id=chunk) for chunk in chunks),
            context=context,
        )
        by_point = {
            row.id: row
            for row in records
            if str(row.tenant_id) == str(context.tenant_id)
            and row.payload.get("record_kind") == "evidence"
            and row.payload.get("chunk_id") in set(chunks)
        }
        # Preserve the entity ranking: a chunk's position here is its entity's
        # rank, not its own similarity, which is the point of the lane.
        return tuple(
            VectorSearchResult(id=row.id, score=0, raw_score=0, payload=row.payload)
            for chunk in chunks
            if (row := by_point.get(identity.point_id(chunk_id=chunk))) is not None
        )
