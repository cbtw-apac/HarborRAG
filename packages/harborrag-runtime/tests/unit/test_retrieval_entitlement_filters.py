"""The datasource set a request is entitled to narrows vector ranking and entity search."""

from __future__ import annotations

import pytest
from test_entity_summary_search import (
    PROFILE,
    Summaries,
    Vectors,
    entity_hit,
    evidence_record,
)
from test_find_entities_tool import Released

from harborrag_core.contracts.reader import EntityFindRequest
from harborrag_core.indexing import (
    FilterOperator,
    VectorFilter,
    VectorFilterCondition,
    VectorSearchQuery,
    VectorSearchResult,
)
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.retrieval.entity_summary import EntitySummarySearch
from harborrag_runtime.retrieval.permissions import RetrievalPermissions

ENGINEERING = "scope-engineering"
HR = "scope-hr"


class AclTopology:
    """The canonical authority: it enumerates the documents a principal may read."""

    async def allowed_document_ids(self, tenant_id, *, access, limit):
        return ("doc-1", "doc-2")


class SharedTopology:
    """A tenant-shared corpus never enumerates documents; asking is a bug."""

    async def allowed_document_ids(self, *args, **kwargs):
        raise AssertionError("tenant_shared mode must not enumerate documents")


def _access(
    entitled: frozenset[str] | None,
    *,
    corpus_mode: str = "tenant_shared",
) -> AccessContext:
    return AccessContext(
        principal_id="reader",
        tenant_id="DEFAULT",
        corpus_mode=corpus_mode,  # type: ignore[arg-type]
        entitled_source_scopes=entitled,
    )


def _context(access: AccessContext) -> StorageOperationContext:
    return StorageOperationContext.for_access(
        access, operation_kind="retrieval", idempotency_key="test"
    )


def _condition(field: str, value: object, operator: FilterOperator = FilterOperator.EQUALS):
    return VectorFilterCondition(field=field, operator=operator, value=value)


def _scope_condition(*scopes: str) -> VectorFilterCondition:
    return _condition("source_scope_id", list(scopes), FilterOperator.IN)


_CALLER = VectorFilter(
    must=[_condition("language", "en")],
    should=[_condition("title", "Guide")],
    must_not=[_condition("status", "draft")],
)


@pytest.mark.asyncio
async def test_shared_scope_filter_adds_the_datasource_condition() -> None:
    permissions = RetrievalPermissions(SharedTopology())  # type: ignore[arg-type]
    context = _context(_access(frozenset({ENGINEERING})))

    scoped = await permissions.scope_filter(_CALLER, context)

    assert scoped == VectorFilter(
        must=[*_CALLER.must, _scope_condition(ENGINEERING)],
        should=list(_CALLER.should),
        must_not=list(_CALLER.must_not),
    )
    # With no caller filter at all, the datasource condition is the whole filter.
    assert await permissions.scope_filter(None, context) == VectorFilter(
        must=[_scope_condition(ENGINEERING)]
    )


@pytest.mark.asyncio
async def test_source_acl_scope_filter_adds_both_conditions() -> None:
    permissions = RetrievalPermissions(AclTopology())  # type: ignore[arg-type]
    context = _context(_access(frozenset({HR, ENGINEERING}), corpus_mode="source_acl"))

    scoped = await permissions.scope_filter(_CALLER, context)

    assert scoped == VectorFilter(
        must=[
            *_CALLER.must,
            _condition("document_id", ["doc-1", "doc-2"], FilterOperator.IN),
            # Sorted, so the same entitlement is always the same filter.
            _scope_condition(ENGINEERING, HR),
        ],
        should=list(_CALLER.should),
        must_not=list(_CALLER.must_not),
    )


@pytest.mark.asyncio
async def test_scope_filter_unchanged_without_entitlements() -> None:
    shared = _context(_access(None))
    acl = _context(_access(None, corpus_mode="source_acl"))

    assert await RetrievalPermissions(SharedTopology()).scope_filter(None, shared) is None  # type: ignore[arg-type]
    assert await RetrievalPermissions(SharedTopology()).scope_filter(_CALLER, shared) is _CALLER  # type: ignore[arg-type]
    assert await RetrievalPermissions(AclTopology()).scope_filter(_CALLER, acl) == VectorFilter(  # type: ignore[arg-type]
        must=[*_CALLER.must, _condition("document_id", ["doc-1", "doc-2"], FilterOperator.IN)],
        should=list(_CALLER.should),
        must_not=list(_CALLER.must_not),
    )


@pytest.mark.asyncio
async def test_a_legacy_embedding_only_caller_keeps_its_filter() -> None:
    context = _context(_access(frozenset({ENGINEERING})))

    assert await RetrievalPermissions(None).scope_filter(_CALLER, context) is _CALLER


class RecordingVectors(Vectors):
    """``Vectors`` that keeps every query it was asked to rank."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.queries: list[VectorSearchQuery] = []

    async def search(self, query, *, context):
        self.queries.append(query)
        return await super().search(query, context=context)


def _ranked_scopes(vectors: RecordingVectors) -> set[str]:
    """Every datasource any recorded ranking was allowed to reach."""

    return {
        scope
        for query in vectors.queries
        if query.filters is not None
        for condition in query.filters.must
        if condition.field == "source_scope_id"
        for scope in condition.value
    }


def _find_request(
    entitled: frozenset[str] | None, *scopes: str, corpus_mode: str = "tenant_shared"
) -> EntityFindRequest:
    return EntityFindRequest(
        access=_access(entitled, corpus_mode=corpus_mode),
        query="java",
        source_scope_ids=tuple(scopes),
    )


async def _find(search: EntitySummarySearch, request: EntityFindRequest):
    return await search.find(request, "entities-x", (1.0, 0.0), _context(request.access))


def _entity_search(released: set[str], hits=(entity_hit("node-a"),)):
    vectors = RecordingVectors(records={PROFILE.entity_index_name: {}}, hits=hits)
    return EntitySummarySearch(Released(released), vectors, PROFILE), vectors


@pytest.mark.asyncio
async def test_find_entities_narrows_requested_scopes() -> None:
    search, vectors = _entity_search({"node-a"})

    await _find(search, _find_request(frozenset({ENGINEERING}), ENGINEERING, HR))

    assert _ranked_scopes(vectors) == {ENGINEERING}


@pytest.mark.asyncio
async def test_find_entities_without_requested_scopes_ranks_within_the_entitled_set() -> None:
    search, vectors = _entity_search({"node-a"})

    response = await _find(search, _find_request(frozenset({HR, ENGINEERING})))

    assert _ranked_scopes(vectors) == {ENGINEERING, HR}
    assert [match.node_key for match in response.matches] == ["node-a"]


@pytest.mark.asyncio
async def test_find_entities_naming_only_other_datasources_ranks_nothing() -> None:
    search, vectors = _entity_search(set())

    response = await _find(search, _find_request(frozenset({ENGINEERING}), HR))

    assert vectors.queries == []
    assert response.matches == ()
    assert response.reasons == ()
    assert response.withheld_count == 0
    assert not response.truncated


@pytest.mark.asyncio
async def test_find_entities_keeps_the_requested_scopes_without_entitlements() -> None:
    search, vectors = _entity_search({"node-a"})

    await _find(search, _find_request(None, ENGINEERING, HR))

    assert _ranked_scopes(vectors) == {ENGINEERING, HR}


@pytest.mark.asyncio
async def test_find_entities_does_not_count_hits_withheld_from_an_entitled_principal() -> None:
    """A count under an entitlement would say how many other datasources' entities match."""

    hits = (entity_hit("node-a"), entity_hit("node-b"))
    search, _ = _entity_search({"node-b"}, hits)

    entitled = await _find(search, _find_request(frozenset({ENGINEERING}), ENGINEERING))
    ungated = await _find(search, _find_request(None, ENGINEERING))

    assert [match.node_key for match in entitled.matches] == ["node-b"]
    assert entitled.reasons == () and entitled.withheld_count == 0
    # The same hits on a corpus with no entitlement set keep reporting what was withheld.
    assert [match.node_key for match in ungated.matches] == ["node-b"]
    assert ungated.withheld_count == 1


@pytest.mark.asyncio
async def test_the_entity_lane_inside_retrieval_ranks_within_the_entitled_set() -> None:
    chunk = "chunk-1"
    record = evidence_record(chunk)
    vectors = RecordingVectors(
        records={PROFILE.entity_index_name: {}, "evidence": {record.id: record}},
        hits=(entity_hit("node-a"),),
    )
    search = EntitySummarySearch(Summaries({"node-a": (chunk,)}), vectors, PROFILE)
    existing = VectorSearchResult(
        id="existing", score=0.5, raw_score=0.5, payload={"chunk_id": "c"}
    )
    context = _context(_access(frozenset({ENGINEERING})))

    fused = await search.expand((existing,), (1.0, 0.0), context=context)

    assert _ranked_scopes(vectors) == {ENGINEERING}
    # Enrichment, not selection: the chunk candidates stay and the entity's evidence joins.
    assert {str(item.id) for item in fused} == {"existing", str(record.id)}


@pytest.mark.asyncio
async def test_the_entity_lane_is_unrestricted_without_entitlements() -> None:
    vectors = RecordingVectors(records={PROFILE.entity_index_name: {}}, hits=())
    search = EntitySummarySearch(Summaries({}), vectors, PROFILE)

    await search.expand((), (1.0, 0.0), context=_context(_access(None)))

    assert vectors.queries and _ranked_scopes(vectors) == set()
