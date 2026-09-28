"""find_entities: whole entities ranked and filtered, evidence released by the authority."""

import pytest
from test_entity_summary_search import PROFILE, Vectors, entity_hit

from harborrag_core.contracts.reader import (
    EntityFindRequest,
    EntityFindResponse,
    EntityMatch,
)
from harborrag_core.indexing import VectorFilter, VectorFilterCondition
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.tools.catalog import build_reader_tool_catalog
from harborrag_engine.tools.find_entities import FindEntitiesTool
from harborrag_engine.tools.references import KnowledgeReferenceStore
from harborrag_runtime.retrieval.entity_summary import EntitySummarySearch


class Retrieval:
    def __init__(self) -> None:
        self.requests: list[EntityFindRequest] = []

    async def search(self, request):  # pragma: no cover - not exercised here
        raise AssertionError("search is not the tool under test")

    async def find_entities(self, request: EntityFindRequest) -> EntityFindResponse:
        self.requests.append(request)
        return EntityFindResponse(
            "entities-1",
            (
                EntityMatch(
                    node_key="node-a",
                    source_scope_id="AUTA-2",
                    rank=1,
                    score=0.91,
                    description="Senior Java engineer, banking, cleared round two.",
                    coverage_mode="partial",
                    attributes=(
                        {
                            "name": "stage",
                            "values": ["round 2"],
                            "from_document_ids": ["doc-issue"],
                        },
                    ),
                    evidence_chunk_ids=("chunk-1", "chunk-2"),
                ),
            ),
            truncated=True,
        )


@pytest.mark.asyncio
async def test_the_tool_passes_facets_through_and_reports_partial_coverage() -> None:
    retrieval = Retrieval()
    tool = FindEntitiesTool(retrieval=retrieval, references=KnowledgeReferenceStore())
    result = await tool.call(
        {
            "tenant_id": "AUTA-2",
            "query": "senior java engineer with banking experience",
            "facets": {"stage": "round 2", "years_experience": {"gte": 5}},
            "source_ids": ["AUTA-2"],
            "limit": 5,
        },
        principal_id="recruiter-1",
    )
    request = retrieval.requests[0]
    assert request.facets == {"stage": "round 2", "years_experience": {"gte": 5}}
    assert request.source_scope_ids == ("AUTA-2",)
    assert request.limit == 5
    assert str(request.access.tenant_id) == "AUTA-2"
    assert result["ok"] is True
    match = result["matches"][0]
    assert match["node_key"] == "node-a"
    assert match["coverage_mode"] == "partial"
    assert match["attributes"][0]["values"] == ["round 2"]
    assert match["evidence_chunk_ids"] == ["chunk-1", "chunk-2"]
    # More qualified than the limit: the caller is told, not left to assume.
    assert result["completion"] == {"complete": False, "reasons": ["candidate_limit"]}


@pytest.mark.asyncio
async def test_bad_arguments_and_a_missing_backend_are_failures_not_exceptions() -> None:
    tool = FindEntitiesTool(retrieval=Retrieval(), references=KnowledgeReferenceStore())
    blank = await tool.call({"tenant_id": "AUTA-2", "query": "  "}, principal_id="p")
    assert blank["ok"] is False
    too_many = await tool.call(
        {"tenant_id": "AUTA-2", "query": "q", "limit": 500}, principal_id="p"
    )
    assert too_many["ok"] is False
    unconfigured = FindEntitiesTool(references=KnowledgeReferenceStore())
    missing = await unconfigured.call({"tenant_id": "AUTA-2", "query": "q"}, principal_id="p")
    assert missing["ok"] is False and "not configured" in missing["error"]


def test_the_tool_is_in_the_shared_catalog_and_read_only() -> None:
    tools = build_reader_tool_catalog(None, KnowledgeReferenceStore())
    spec = next(tool.spec for tool in tools if tool.spec.name == "find_entities")
    assert spec.behavior.read_only and spec.behavior.idempotent
    assert set(spec.input_schema["required"]) == {"tenant_id", "query"}
    assert "facets" in spec.input_schema["properties"]


class Summaries:
    async def entity_evidence(self, tenant_id, node_keys, *, access):
        return {}

    async def views(self, *args, **kwargs):
        return {}


@pytest.mark.asyncio
async def test_ranked_hits_carry_scope_and_coverage_and_drop_foreign_points() -> None:
    base = entity_hit("node-a")
    good = base.model_copy(update={"payload": {**base.payload, "coverage_mode": "complete"}})
    other = entity_hit("node-b")
    foreign = other.model_copy(
        update={"payload": {**other.payload, "record_kind": "parent_description"}}
    )
    vectors = Vectors(records={PROFILE.entity_index_name: {}}, hits=(good, foreign, good))
    search = EntitySummarySearch(Summaries(), vectors, PROFILE)
    hits = await search.ranked_entities(
        (1.0, 0.0),
        StorageOperationContext.system("DEFAULT"),
        VectorFilter(must=[VectorFilterCondition(field="facet.stage", value="round 2")]),
    )
    assert [(hit.node_key, hit.source_scope_id, hit.coverage_mode) for hit in hits] == [
        ("node-a", "scope", "complete")
    ]
