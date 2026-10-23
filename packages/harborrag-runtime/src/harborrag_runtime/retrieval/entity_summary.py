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
import re
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
from harborrag_core.summaries import FACET_NAME_PATTERN, SummaryCard
from harborrag_core.topology.derived import (
    ENTITY_SUMMARY_RECORD_KIND,
    ContextualIndexProfile,
)
from harborrag_engine.retrieval.fusion import fuse_candidates

from .source_fields import PayloadIndexCatalog

_MAX_ENTITIES = 8
_MAX_CHUNKS_PER_ENTITY = 8
# With facet filters the lane is doing selection, not enrichment: the caller has
# said which entities count, so the ceiling is how many of them we will rank.
_MAX_SELECTED_ENTITIES = 50
FACET_FILTER_PREFIX = "facet."
# A facet named that no source scope declared: the filter cannot match anything,
# so the empty answer it produces is not a conclusive one.
UNKNOWN_FACET = "unknown_facet"
_FACET_NAME = re.compile(FACET_NAME_PATTERN)


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
    caller's ``Java`` finds a card's ``java``. A name that no scope could have
    declared (``Skill Set`` instead of ``skill_set``) is refused, not searched.
    """

    must: list[VectorFilterCondition] = []
    for name, value in sorted(facets.items()):
        if not _FACET_NAME.fullmatch(name):
            raise ValueError(
                f"facet {name!r} is not a facet name; use the names list_sources gives "
                "as entity_facets"
            )
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


def _folded_condition(condition: VectorFilterCondition) -> VectorFilterCondition:
    """Fold a ``facet.*`` condition's text the way ``facet_filter_from_mapping`` does."""

    value = condition.value
    folded = (
        [_folded(item) for item in value] if isinstance(value, list | tuple) else _folded(value)
    )
    return condition.model_copy(update={"value": folded})


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
    it has no conditions. Facet text is case-folded on the way, as it was stored,
    so ``facet.stage == "Round 2"`` matches here exactly as it does in
    ``find_entities``.
    """

    if filters is None:
        return None, None
    facet: dict[str, list[VectorFilterCondition]] = {"must": [], "should": [], "must_not": []}
    rest: dict[str, list[VectorFilterCondition]] = {"must": [], "should": [], "must_not": []}
    for clause in facet:
        for condition in getattr(filters, clause):
            if condition.field.startswith(FACET_FILTER_PREFIX):
                facet[clause].append(_folded_condition(condition))
            else:
                rest[clause].append(condition)
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
        chunk_filter: VectorFilter | None = None,
    ) -> tuple[VectorSearchResult, ...]:
        """Fuse entity-reached evidence into the candidate set -- or replace it.

        Without facet filters this is enrichment: entity hits join the chunk
        candidates by rank fusion. With them it is selection: the caller has said
        which entities qualify, so only evidence from qualifying entities may be
        returned, and the chunk candidates are set aside.

        ``chunk_filter`` is the rest of the caller's filter -- scope, type, dates,
        the items a ``fields.*`` filter traced. Entity-reached evidence must meet
        it like every other candidate: reaching a chunk through its entity is not
        a way around what the caller asked for.

        Returns the input unchanged on every path that cannot be served safely,
        so a missing index, an empty vector or an unreadable binding degrades to
        plain chunk retrieval instead of failing the request.
        """

        if dense_vector is None or len(dense_vector) != self.profile.dimension:
            return candidates if facet_filter is None else ()
        node_keys = await self.matching_entities(
            dense_vector,
            context,
            _with_scope_conditions(facet_filter, chunk_filter),
            # The scope conditions narrow the ranking; they do not turn
            # enrichment into selection, so the mode's own ceiling stays.
            limit=_MAX_SELECTED_ENTITIES if facet_filter is not None else self.max_entities,
        )
        reached = tuple(
            item
            for item in await self._evidence_for(node_keys, context)
            if chunk_filter is None or payload_matches(item.payload, chunk_filter)
        )
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
            unknown = await self._undeclared_facets(request.facets, context)
            return EntityFindResponse(request_id, (), reasons=(UNKNOWN_FACET,) if unknown else ())
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

    async def _undeclared_facets(
        self, facets: Mapping[str, object], context: StorageOperationContext
    ) -> tuple[str, ...]:
        """The facet names the entity index has no payload index for.

        Every facet any scope declares is indexed when its cards are published,
        so a name missing from the index was declared nowhere: a filter on it can
        only match nothing, which is not the same answer as "no entity qualifies".
        The cached index list is refreshed once before calling a name unknown.
        """

        if not facets or not isinstance(self.vectors, PayloadIndexCatalog):
            return ()
        index = self.profile.entity_index_name
        wanted = {f"{FACET_FILTER_PREFIX}{name}" for name in facets}
        indexed = await self.vectors.indexed_payload_fields(index, context=context)
        if not wanted <= indexed:
            indexed = await self.vectors.indexed_payload_fields(
                index, refresh=True, context=context
            )
        return tuple(sorted(wanted - indexed))

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


def _with_scope_conditions(
    facet_filter: VectorFilter | None, chunk_filter: VectorFilter | None
) -> VectorFilter | None:
    """Carry the caller's ``source_scope_id`` conditions onto the entity search.

    Entity points carry their scope, so ranking within it is a lookup. Without
    it, selection ranked the top entities of every scope and then dropped the
    ones outside the caller's, which could leave nothing although entities of
    the requested scope qualified.
    """

    def scoped(conditions: list[VectorFilterCondition]) -> list[VectorFilterCondition]:
        return [
            item
            for item in conditions
            if item.field == "source_scope_id"
            and item.operator in {FilterOperator.EQUALS, FilterOperator.IN}
        ]

    if chunk_filter is None:
        return facet_filter
    must, must_not = scoped(chunk_filter.must), scoped(chunk_filter.must_not)
    if not must and not must_not:
        return facet_filter
    base = facet_filter or VectorFilter()
    return VectorFilter(
        must=[*base.must, *must], should=list(base.should), must_not=[*base.must_not, *must_not]
    )


def payload_matches(payload: Mapping[str, object], filters: VectorFilter) -> bool:
    """Whether an evidence payload satisfies a filter, read the way Qdrant reads it.

    Entity-reached chunks are fetched by id, not searched, so the caller's filter
    is checked here instead. A dotted field walks nested objects, a list-valued
    field matches when any member does, and ``should`` needs one of its
    conditions when it has any. An operator this cannot evaluate fails closed.
    """

    return (
        all(_condition_matches(payload, item) for item in filters.must)
        and (not filters.should or any(_condition_matches(payload, i) for i in filters.should))
        and not any(_condition_matches(payload, item) for item in filters.must_not)
    )


_COMPARISONS = {
    FilterOperator.GREATER_THAN: lambda value, bound: value > bound,
    FilterOperator.GREATER_THAN_OR_EQUAL: lambda value, bound: value >= bound,
    FilterOperator.LESS_THAN: lambda value, bound: value < bound,
    FilterOperator.LESS_THAN_OR_EQUAL: lambda value, bound: value <= bound,
}


def _condition_matches(payload: Mapping[str, object], condition: VectorFilterCondition) -> bool:
    values = _payload_values(payload, condition.field)
    operator, wanted = condition.operator, condition.value
    if operator == FilterOperator.EXISTS:
        return bool(values) is (wanted is not False)
    if operator in {FilterOperator.EQUALS, FilterOperator.NOT_EQUALS}:
        found = any(_same(value, wanted) for value in values)
        return found if operator == FilterOperator.EQUALS else not found
    if operator in {FilterOperator.IN, FilterOperator.NOT_IN}:
        options = wanted if isinstance(wanted, list | tuple) else ()
        found = any(_same(value, option) for value in values for option in options)
        return found if operator == FilterOperator.IN else not found
    compare = _COMPARISONS.get(operator)
    if compare is None or not _is_number(wanted):
        return False
    return any(_is_number(value) and compare(value, wanted) for value in values)


def _payload_values(payload: Mapping[str, object], field: str) -> tuple[object, ...]:
    value: object = payload
    for part in field.split("."):
        if not isinstance(value, Mapping):
            return ()
        value = value.get(part)
    if value is None:
        return ()
    return tuple(value) if isinstance(value, list | tuple) else (value,)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _same(value: object, wanted: object) -> bool:
    # ``True == 1`` in Python, never in a payload match.
    if isinstance(value, bool) or isinstance(wanted, bool):
        return isinstance(value, bool) and isinstance(wanted, bool) and value is wanted
    return value == wanted
