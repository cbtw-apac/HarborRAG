"""The entity lane honours the caller's whole filter, not only its ``facet.*`` part."""

from __future__ import annotations

import pytest
from test_entity_summary_search import PROFILE, Vectors, entity_hit, evidence_record

from harborrag_core.indexing import (
    FilterOperator,
    VectorFilter,
    VectorFilterCondition,
    VectorIndexRecord,
    VectorSearchResult,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.retrieval.entity_summary import (
    EntitySummarySearch,
    facet_filter_from_mapping,
    payload_matches,
    split_facet_filters,
)
from harborrag_runtime.retrieval.facades import _build_vector_filter
from harborrag_runtime.retrieval.source_fields import SourceFieldTrace

CONTEXT = StorageOperationContext.system("DEFAULT")


class Summaries:
    def __init__(self, released: dict[str, tuple[str, ...]]):
        self.released = released

    async def entity_evidence(self, tenant_id, node_keys, *, access):
        return {key: self.released[key] for key in node_keys if key in self.released}

    async def views(self, *args, **kwargs):  # pragma: no cover - unused here
        return {}


class RecordingVectors(Vectors):
    def __init__(self, records=None, hits=()):
        super().__init__(records=records, hits=hits)
        self.queries = []

    async def search(self, query, *, context):
        self.queries.append(query)
        return list(self.hits)


def _evidence(chunk: str, **payload: object) -> VectorIndexRecord:
    base = evidence_record(chunk)
    return base.model_copy(update={"payload": {**base.payload, **payload}})


def _candidate(record: VectorIndexRecord) -> VectorSearchResult:
    return VectorSearchResult(id=record.id, score=1.0, raw_score=1.0, payload=record.payload)


def _search(records: tuple[VectorIndexRecord, ...], released, hits) -> tuple:
    vectors = RecordingVectors(
        records={
            PROFILE.entity_index_name: {},
            "evidence": {row.id: row for row in records},
        },
        hits=hits,
    )
    return EntitySummarySearch(Summaries(released), vectors, PROFILE), vectors


@pytest.mark.asyncio
async def test_enrichment_keeps_the_callers_scope_filter() -> None:
    hr2025 = _evidence("hr2025-chunk", source_scope_id="HR-2025")
    hr2024 = _evidence("hr2024-chunk", source_scope_id="HR-2024")
    search, vectors = _search(
        (hr2025, hr2024),
        {"node-hr2024": ("hr2024-chunk",)},
        (entity_hit("node-hr2024"),),
    )
    scope = VectorFilter(must=[VectorFilterCondition(field="source_scope_id", value="HR-2025")])

    out = await search.expand(
        (_candidate(hr2025),), (1.0, 0.0), context=CONTEXT, chunk_filter=scope
    )

    assert {row.payload["source_scope_id"] for row in out} == {"HR-2025"}
    # The scope is pushed into the entity ranking too, and the lane stays enrichment.
    query = vectors.queries[-1]
    assert [c.field for c in query.filters.must] == ["embedding_profile", "source_scope_id"]
    assert query.top_k == search.max_entities


@pytest.mark.asyncio
async def test_selection_keeps_only_entity_evidence_the_fields_trace_allows() -> None:
    banking = _evidence("banking-chunk", source_item_id="AUTA-1")
    retail = _evidence("retail-chunk", source_item_id="AUTA-2")
    search, _ = _search(
        (banking, retail),
        {"node-a": ("banking-chunk",), "node-b": ("retail-chunk",)},
        (entity_hit("node-a"), entity_hit("node-b")),
    )
    facet = VectorFilter(must=[VectorFilterCondition(field="facet.stage", value="round 2")])
    # What retrieve() builds from ``fields.skill_set == banking`` once traced to AUTA-1.
    traced = SourceFieldTrace.scope(("AUTA-1",), None)

    out = await search.expand(
        (_candidate(banking),),
        (1.0, 0.0),
        context=CONTEXT,
        facet_filter=facet,
        chunk_filter=traced,
    )

    assert [row.payload["chunk_id"] for row in out] == ["banking-chunk"]


def test_retrieve_folds_facet_values_like_find_entities() -> None:
    facet, rest = split_facet_filters(
        _build_vector_filter({"facet.stage": "Round 2", "facet.skills": ["Java ", "K8s"]})
    )
    by_field = {condition.field: condition.value for condition in facet.must}
    assert by_field["facet.stage"] == facet_filter_from_mapping({"stage": "Round 2"}).must[0].value
    assert by_field["facet.stage"] == "round 2"
    assert by_field["facet.skills"] == ["java", "k8s"]
    assert rest is None


def test_a_payload_is_read_the_way_the_vector_store_reads_it() -> None:
    payload = {
        "source_scope_id": "HR-2025",
        "labels": ["java", "python"],
        "fields": {"years": 7, "remote": True},
    }

    def matches(*must, should=(), must_not=()) -> bool:
        return payload_matches(
            payload, VectorFilter(must=list(must), should=list(should), must_not=list(must_not))
        )

    def cond(field, value, operator=FilterOperator.EQUALS):
        return VectorFilterCondition(field=field, operator=operator, value=value)

    assert matches(cond("labels", "java"))
    assert matches(cond("labels", ["go", "python"], FilterOperator.IN))
    assert not matches(cond("labels", "go"))
    assert matches(cond("fields.years", 5, FilterOperator.GREATER_THAN_OR_EQUAL))
    assert not matches(cond("fields.years", 7, FilterOperator.GREATER_THAN))
    # A boolean is not the number one.
    assert not matches(cond("fields.remote", 1))
    assert matches(should=[cond("source_scope_id", "X"), cond("source_scope_id", "HR-2025")])
    assert not matches(should=[cond("source_scope_id", "X")])
    assert not matches(must_not=[cond("source_scope_id", "HR-2025")])
    assert not matches(cond("missing", "x"))


class RecordingEntities:
    """Stands in for the entity lane and records what retrieve() hands it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def expand(self, candidates, dense_vector, *, context, facet_filter, chunk_filter):
        self.calls.append({"facet_filter": facet_filter, "chunk_filter": chunk_filter})
        return candidates


@pytest.mark.asyncio
async def test_retrieve_hands_the_entity_lane_the_rest_of_the_callers_filter() -> None:
    from dataclasses import replace

    from retrieval_test_support import policy, resources
    from topology_retrieval_support import ActiveVersions, CanonicalTopology, TopologyVectors

    from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService

    entities = RecordingEntities()
    service = RuntimeRetrievalService(
        resources=replace(
            resources(vectors=TopologyVectors(), active_versions=ActiveVersions()),
            topology_repository=CanonicalTopology(),
            entity_summary_search=entities,
        ),
        policy=policy(),
    )
    await service.retrieve(
        "question",
        tenant_id="tenant-1",
        options=RetrievalOptions(
            filters=_build_vector_filter({"source_scope_id": "HR-2025", "facet.stage": "Round 2"})
        ),
    )

    (call,) = entities.calls
    assert [(c.field, c.value) for c in call["facet_filter"].must] == [("facet.stage", "round 2")]
    # The chunk filter the lane receives is the caller's, not the permission-scoped one.
    assert [(c.field, c.value) for c in call["chunk_filter"].must] == [
        ("source_scope_id", "HR-2025")
    ]
