"""Filters fail fast instead of scanning, and identical texts are collapsed.

On a 390k-point tenant an unindexed ``fields.*`` filter ran the full 30 s
request deadline and then failed; an unknown key did the same silently. Both
are refused before any search. And identical short comments, which embed
identically, no longer fill a whole page.
"""

from __future__ import annotations

import pytest
from retrieval_test_support import FakeChunkReader, FakeEmbedClient, FakeVectorRepository
from retrieval_test_support import policy as _policy
from retrieval_test_support import resources as _resources

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.indexing import VectorFilter, VectorFilterCondition, VectorSearchResult
from harborrag_runtime.config.graph_build import GraphBuildConfig
from harborrag_runtime.ingestion.field_indexes import declared_field_indexes
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService

ISSUE = "jira://CPM/CPM-116680"


class IndexedVectors(FakeVectorRepository):
    """A backend that reports its payload indexes, as the Qdrant adapter does."""

    def __init__(self, indexed: frozenset[str], later: frozenset[str] | None = None) -> None:
        super().__init__()
        self.indexed = indexed
        self.later = later
        self.refreshes = 0
        self.facets: list[str] = []

    async def indexed_payload_fields(self, index_name, *, refresh=False, context):
        del index_name, context
        if refresh:
            self.refreshes += 1
            if self.later is not None:
                self.indexed = self.later
        return self.indexed

    async def distinct_values(self, index_name, field, *, filters=None, limit, context):
        del index_name, filters, limit, context
        self.facets.append(field)
        return (ISSUE,)

    @property
    def queries(self):
        return [*self.dense_queries, *self.sparse_queries, *self.hybrid_queries]


class RepeatingVectors(FakeVectorRepository):
    """Three copies of one comment from different issues, then a distinct hit."""

    def _results(self, collection: str) -> list[VectorSearchResult]:
        if "evidence" not in collection:
            return []
        texts = [("Senior Java Engineer", "same")] * 3 + [("Spring Boot microservices", "other")]
        return [
            VectorSearchResult(
                id=f"point-{index}",
                score=0.9 - index * 0.01,
                raw_score=0.9,
                relevance=0.5,
                payload={
                    "chunk_id": f"chunk-{index}",
                    "document_id": "document-1",
                    "document_version_id": "version-1",
                    "record_kind": "evidence",
                    "chunk_kind": "comment",
                    "connector_type": "jira",
                    "content": text,
                    "content_hash": content_hash,
                    "issue_key": f"CPM-{index}",
                },
            )
            for index, (text, content_hash) in enumerate(texts)
        ]


def _service(vectors: FakeVectorRepository) -> RuntimeRetrievalService:
    return RuntimeRetrievalService(
        resources=_resources(embed=FakeEmbedClient(), vectors=vectors, chunks=FakeChunkReader()),
        policy=_policy(),
    )


def _skill_set() -> RetrievalOptions:
    return RetrievalOptions(
        filters=VectorFilter(
            must=[VectorFilterCondition(field="fields.skill_set", value="FS-Node-React")]
        )
    )


@pytest.mark.asyncio
async def test_an_unindexed_source_field_fails_before_any_scan():
    vectors = IndexedVectors(frozenset({"source_item_id", "fields.position_level"}))

    with pytest.raises(HarborValidationError, match="no payload index") as caught:
        await _service(vectors).retrieve(
            "node developer", tenant_id="tenant-1", top_k=5, options=_skill_set()
        )

    assert caught.value.details == {
        "unindexed": ["fields.skill_set"],
        "indexed": ["fields.position_level"],
    }
    # Refreshed once in case an operator added it, then refused without searching.
    assert vectors.refreshes == 1
    assert vectors.facets == []
    assert vectors.queries == []


@pytest.mark.asyncio
async def test_an_index_added_since_the_cache_was_filled_is_honoured():
    vectors = IndexedVectors(
        frozenset({"source_item_id"}), later=frozenset({"source_item_id", "fields.skill_set"})
    )

    await _service(vectors).retrieve(
        "node developer", tenant_id="tenant-1", top_k=5, options=_skill_set()
    )

    assert vectors.facets == ["source_item_id"]
    assert len(vectors.queries) == 1


@pytest.mark.asyncio
async def test_an_unknown_filter_key_is_refused_before_searching():
    vectors = FakeVectorRepository()

    with pytest.raises(HarborValidationError, match="unsupported evidence filter"):
        await _service(vectors).retrieve(
            "anything",
            tenant_id="tenant-1",
            top_k=5,
            options=RetrievalOptions(
                filters=VectorFilter(
                    must=[VectorFilterCondition(field="document_title", value="Hung Tran")]
                )
            ),
        )

    assert vectors.dense_queries == vectors.hybrid_queries == []


@pytest.mark.asyncio
async def test_identical_texts_are_collapsed_and_counted():
    report = await _service(RepeatingVectors()).retrieve(
        "java developer spring boot", tenant_id="tenant-1", top_k=5
    )

    assert [result.text for result in report.results] == [
        "Senior Java Engineer",
        "Spring Boot microservices",
    ]
    assert report.results[0].id == "chunk-0"
    assert report.results[0].metadata["issue_key"] == "CPM-0"
    assert report.diagnostics.duplicates_collapsed == 2


def test_declared_facets_become_typed_source_field_indexes():
    config = GraphBuildConfig.model_validate(
        {
            "tenants": [
                {
                    "tenant_id": "AUTA-5",
                    "sources": [
                        {
                            "source_scope_id": "AUTA-5",
                            "facets": [
                                {"name": "skill_set", "field": "Skill Set"},
                                {
                                    "name": "years",
                                    "field": "Years of experience",
                                    "type": "integer",
                                },
                                # A standard attribute: already indexed at the top level.
                                {"name": "stage", "field": "status"},
                            ],
                        }
                    ],
                },
                {"tenant_id": "DEFAULT"},
            ]
        },
        strict=False,
    )

    declared = declared_field_indexes(config)

    assert set(declared) == {"AUTA-5"}
    assert [(item.path, item.numeric) for item in declared["AUTA-5"]] == [
        ("fields.skill_set", False),
        ("fields.years_of_experience", True),
    ]
