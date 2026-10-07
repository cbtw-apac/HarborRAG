"""A ``fields.*`` filter reaches an issue's attachments through the issue.

The typed custom fields live on the Jira issue; the CV attached to it is a
document of its own and carries none of them. Retrieval resolves the filter to
the matching issues, then searches their evidence and their attachments'.
"""

from __future__ import annotations

import pytest
from retrieval_test_support import FakeChunkReader, FakeEmbedClient, FakeVectorRepository
from retrieval_test_support import policy as _policy
from retrieval_test_support import resources as _resources

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.indexing import FilterOperator, VectorFilter, VectorFilterCondition
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService
from harborrag_runtime.retrieval.source_fields import SourceFieldTrace, split_field_filters

ISSUE = "jira://CPM/CPM-116680"


class TracingVectors(FakeVectorRepository):
    def __init__(self, items: tuple[str, ...]) -> None:
        super().__init__()
        self.items = items
        self.facets: list[tuple[str, str, VectorFilter | None, int]] = []

    async def distinct_values(self, index_name, field, *, filters=None, limit, context):
        del context
        self.facets.append((index_name, field, filters, limit))
        return self.items

    @property
    def queries(self):
        return [*self.dense_queries, *self.sparse_queries, *self.hybrid_queries]


def _service(vectors: TracingVectors) -> RuntimeRetrievalService:
    return RuntimeRetrievalService(
        resources=_resources(embed=FakeEmbedClient(), vectors=vectors, chunks=FakeChunkReader()),
        policy=_policy(),
    )


def _filters() -> VectorFilter:
    return VectorFilter(
        must=[
            VectorFilterCondition(field="fields.skill_set", value="Data Engineering"),
            VectorFilterCondition(
                field="fields.years_of_experience",
                operator=FilterOperator.GREATER_THAN_OR_EQUAL,
                value=3,
            ),
            VectorFilterCondition(field="project_key", value="CPM"),
        ]
    )


@pytest.mark.asyncio
async def test_the_fields_filter_selects_issues_then_searches_them_and_their_attachments():
    vectors = TracingVectors((ISSUE,))

    await _service(vectors).retrieve(
        "kubernetes streaming pipelines",
        tenant_id="tenant-1",
        top_k=5,
        options=RetrievalOptions(filters=_filters()),
    )

    # Step one: which issues have those field values -- by their own fields only.
    [(index, field, resolved, _)] = vectors.facets
    assert (index, field) == ("evidence", "source_item_id")
    assert resolved is not None
    assert {c.field for c in resolved.must} == {"fields.skill_set", "fields.years_of_experience"}
    # Step two: their evidence, or evidence attached to them; other conditions still hold.
    [(query, _)] = vectors.queries
    assert {(c.field, tuple(c.value)) for c in query.filters.should} == {
        ("source_item_id", (ISSUE,)),
        ("parent_source_item_id", (ISSUE,)),
    }
    assert [c.field for c in query.filters.must] == ["project_key"]


@pytest.mark.asyncio
async def test_no_matching_issue_returns_nothing_without_searching():
    vectors = TracingVectors(())

    report = await _service(vectors).retrieve(
        "anything",
        tenant_id="tenant-1",
        top_k=5,
        options=RetrievalOptions(filters=_filters()),
    )

    assert report.results == ()
    assert vectors.queries == []


@pytest.mark.asyncio
async def test_a_filter_matching_too_many_issues_asks_to_be_narrowed():
    vectors = TracingVectors(tuple(f"jira://CPM/CPM-{index}" for index in range(10_001)))

    with pytest.raises(HarborValidationError, match="narrow"):
        await _service(vectors).retrieve(
            "anything",
            tenant_id="tenant-1",
            top_k=5,
            options=RetrievalOptions(filters=_filters()),
        )


@pytest.mark.asyncio
async def test_without_a_fields_filter_nothing_is_traced():
    vectors = TracingVectors((ISSUE,))
    only_project = VectorFilter(must=[VectorFilterCondition(field="project_key", value="CPM")])

    await _service(vectors).retrieve(
        "anything", tenant_id="tenant-1", top_k=5, options=RetrievalOptions(filters=only_project)
    )

    assert vectors.facets == []
    [(query, _)] = vectors.queries
    assert query.filters.should == []


def test_split_keeps_each_condition_in_its_clause():
    mixed = VectorFilter(
        must=[
            VectorFilterCondition(field="fields.skill_set", value="Data Engineering"),
            VectorFilterCondition(field="issue_key", value="CPM-1"),
        ],
        must_not=[VectorFilterCondition(field="fields.currency", value="EUR")],
    )

    fields, rest = split_field_filters(mixed)

    assert fields is not None and rest is not None
    assert [c.field for c in fields.must] == ["fields.skill_set"]
    assert [c.field for c in fields.must_not] == ["fields.currency"]
    assert [c.field for c in rest.must] == ["issue_key"]
    assert split_field_filters(None) == (None, None)


def test_the_trace_refuses_to_loosen_a_callers_alternatives():
    alternatives = VectorFilter(should=[VectorFilterCondition(field="status", value="Open")])

    with pytest.raises(HarborValidationError, match="alternative"):
        SourceFieldTrace.scope((ISSUE,), alternatives)
