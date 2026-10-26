"""A facet no source declares is not a conclusive "nothing matched"."""

from __future__ import annotations

import pytest
from test_entity_summary_search import PROFILE, Vectors

from harborrag_core.contracts.reader import EntityFindRequest
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.tools.find_entities import FIND_ENTITIES_SPEC, FindEntitiesTool
from harborrag_engine.tools.references import KnowledgeReferenceStore
from harborrag_runtime.retrieval.entity_summary import (
    UNKNOWN_FACET,
    EntitySummarySearch,
    facet_filter_from_mapping,
)

CONTEXT = StorageOperationContext.system("DEFAULT")


class IndexedVectors(Vectors):
    """An entity index that has payload indexes for the declared facets only."""

    def __init__(self, indexed: frozenset[str], **kwargs):
        super().__init__(**kwargs)
        self.indexed = indexed
        self.refreshes = 0

    async def indexed_payload_fields(self, index_name, *, refresh=False, context):
        self.refreshes += int(refresh)
        return self.indexed


class Summaries:
    async def entity_evidence(self, tenant_id, node_keys, *, access):
        return {}

    async def views(self, *args, **kwargs):
        return {}


def _request(facets: dict[str, object]) -> EntityFindRequest:
    access = AccessContext(tenant_id="DEFAULT", principal_id="p")
    return EntityFindRequest(access=access, query="java", facets=facets)


def _search() -> tuple[EntitySummarySearch, IndexedVectors]:
    vectors = IndexedVectors(
        frozenset({"node_key", "facet.skill_set", "facet.years"}),
        records={PROFILE.entity_index_name: {}},
        hits=(),
    )
    return EntitySummarySearch(Summaries(), vectors, PROFILE), vectors


@pytest.mark.asyncio
async def test_an_undeclared_facet_name_makes_the_empty_answer_incomplete() -> None:
    search, vectors = _search()
    response = await search.find(_request({"skillset": "java"}), "entities-x", (1.0, 0.0), CONTEXT)
    assert response.matches == ()
    assert response.reasons == (UNKNOWN_FACET,)
    # The cached index list was re-read once before calling the name unknown.
    assert vectors.refreshes == 1


@pytest.mark.asyncio
async def test_a_declared_facet_with_no_match_stays_a_complete_empty_answer() -> None:
    search, vectors = _search()
    response = await search.find(_request({"skill_set": "java"}), "entities-x", (1.0, 0.0), CONTEXT)
    assert response.matches == () and response.reasons == ()
    assert vectors.refreshes == 0


@pytest.mark.asyncio
async def test_the_tool_reports_the_unknown_facet_as_incomplete() -> None:
    class Retrieval:
        async def find_entities(self, request):
            search, _ = _search()
            return await search.find(request, "entities-x", (1.0, 0.0), CONTEXT)

    tool = FindEntitiesTool(retrieval=Retrieval(), references=KnowledgeReferenceStore())
    result = await tool.call(
        {"tenant_id": "DEFAULT", "query": "java", "facets": {"skillset": "java"}},
        principal_id="p",
    )
    assert result["completion"] == {"complete": False, "reasons": [UNKNOWN_FACET]}
    assert UNKNOWN_FACET in FIND_ENTITIES_SPEC.description


def test_a_field_name_is_not_a_facet_name() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = FIND_ENTITIES_SPEC.input_schema
    # Valid callers keep working.
    jsonschema.validate(
        {"tenant_id": "t", "query": "q", "facets": {"skill_set": "java", "years": {"gte": 5}}},
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"tenant_id": "t", "query": "q", "facets": {"Skill Set": "j"}}, schema)
    # Direct callers that skip the schema are refused too, not searched.
    with pytest.raises(ValueError, match="not a facet name"):
        facet_filter_from_mapping({"Skill Set": "java"})
