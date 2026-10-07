from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_engine.tools.vector_search import VectorSearchTool
from harborrag_mcp_server.server.server import McpServer
from harborrag_runtime.sdk import RetrievalLane, RetrievalMode


def _result(id_: str, text: str, score: float, relevance: float | None = None) -> RetrievalResult:
    """A ``RetrievalResult`` with the exact metadata shape the real service produces."""
    return RetrievalResult(
        id=id_,
        text=text,
        score=score,
        relevance=relevance,
        metadata={
            "document_id": "document-1",
            "document_version_id": "version-1",
            "record_kind": "evidence",
            "chunk_kind": "text",
            "connector_type": "local",
            "citation_locator": {},
            "quality_score": None,
            "retrieval_source": "qdrant-authoritative",
            "document_title": "Document One",
            "section_path": ["Section 1"],
        },
    )


def _diagnostics(candidate_hits: int) -> dict[str, object]:
    """A full ``RetrievalDiagnostics``-shaped dict, matching what the real service emits."""
    return {
        "candidate_hits": candidate_hits,
        "stale_candidates": 0,
        "unpublished_candidates": 0,
        "malformed_candidates": 0,
        "search_window": candidate_hits,
        "graph_nodes": 0,
        "graph_relations": 0,
        "graph_truncated": False,
        "duration_ms": 0.0,
        "graph_documents": [],
    }


class StaticRetrievalFacade:
    def __init__(self, results: list[RetrievalResult]) -> None:
        self.results = results
        self.last_request = None

    async def search(self, request):
        self.last_request = request
        ordered = sorted(self.results, key=lambda item: item.score, reverse=True)
        return SimpleNamespace(
            request_id="retrieval-1",
            lane=request.lane,
            results=tuple(ordered[: request.top_k]),
            diagnostics=_diagnostics(len(ordered)),
        )


def runtime(results: list[RetrievalResult]):
    retrieval = StaticRetrievalFacade(results)
    return SimpleNamespace(retrieval=retrieval), retrieval


@pytest.mark.asyncio
async def test_vector_search_defaults_to_hybrid_without_graph_observation() -> None:
    harbor, retrieval = runtime([_result("vec-1", "one", 0.95)])

    result = await VectorSearchTool(runtime=harbor).call(
        {"query": "HarborRAG vector", "tenant_id": "demo", "top_k": 1},
        principal_id="subject-1",
    )

    assert result["ok"] is True
    request = retrieval.last_request
    assert request.access.principal_id == "subject-1"
    assert request.access.tenant_id == "demo"
    assert request.lane == RetrievalLane.HYBRID
    assert request.observe_graph is False
    assert result["results"][0]["id"] == "vec-1"


@pytest.mark.asyncio
async def test_vector_search_forwards_explicit_controls_and_threshold() -> None:
    harbor, retrieval = runtime(
        [_result("high", "alpha", 0.9, 0.9), _result("low", "beta", 0.2, 0.2)]
    )

    result = await VectorSearchTool(runtime=harbor).call(
        {
            "query": "alpha",
            "tenant_id": "demo",
            "lane": "dense",
            "mode": "local_semantic",
            "filters": {"status": "runbook"},
            "observe_graph": False,
            "score_threshold": 0.8,
        },
        principal_id="subject-1",
    )

    request = retrieval.last_request
    assert request.lane == RetrievalLane.DENSE
    assert request.mode == RetrievalMode.LOCAL_SEMANTIC
    assert request.filters == {"status": "runbook"}
    assert request.observe_graph is False
    assert [item["id"] for item in result["results"]] == ["high"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": " ", "tenant_id": "demo"},
        {"query": "x", "tenant_id": " ", "top_k": 1},
        {"query": "x", "tenant_id": "demo", "top_k": True},
        {"query": "x", "tenant_id": "demo", "top_k": 21},
        {"query": "x", "tenant_id": "demo", "lane": "invalid"},
        {"query": "x", "tenant_id": "demo", "mode": "global"},
        {"query": "x", "tenant_id": "demo", "filters": "invalid"},
        {"query": "x", "tenant_id": "demo", "score_threshold": True},
    ],
)
async def test_vector_search_rejects_invalid_direct_inputs(arguments) -> None:
    assert (await VectorSearchTool().call(arguments, principal_id="subject-1"))["ok"] is False


def test_vector_search_schema_exposes_all_retrieval_controls() -> None:
    schema = VectorSearchTool.spec.input_schema

    assert schema["required"] == ["query", "tenant_id"]
    assert {
        "query",
        "tenant_id",
        "top_k",
        "lane",
        "filters",
        "observe_graph",
        "score_threshold",
    } <= set(schema["properties"])
    assert schema["properties"]["top_k"]["maximum"] == 20


@pytest.mark.asyncio
async def test_backend_failure_returns_generic_error_but_logs_the_cause(caplog) -> None:
    class RaisingRetrievalFacade:
        async def search(self, request):
            raise RuntimeError("provider config invalid: openai\r")

    harbor = SimpleNamespace(retrieval=RaisingRetrievalFacade())

    with caplog.at_level("ERROR", logger="harborrag.mcp.tools.vector_search"):
        result = await VectorSearchTool(runtime=harbor).call(
            {"query": "HarborRAG vector", "tenant_id": "demo"},
            principal_id="subject-1",
        )

    assert result == {"ok": False, "error": "vector retrieval backend failed"}
    logged = [record for record in caplog.records if record.exc_info is not None]
    assert logged, "the real exception must be logged even though the caller sees a generic error"
    assert "provider config invalid" in str(logged[0].exc_info[1])


@pytest.mark.asyncio
async def test_vector_search_success_and_failure_outputs_match_output_schema() -> None:
    from jsonschema.validators import validator_for

    validator_type = validator_for(VectorSearchTool.spec.output_schema)
    validator_type.check_schema(VectorSearchTool.spec.output_schema)
    validator = validator_type(VectorSearchTool.spec.output_schema)

    harbor, _ = runtime([_result("vec-1", "one", 0.95)])
    success = await VectorSearchTool(runtime=harbor).call(
        {"query": "HarborRAG vector", "tenant_id": "demo"},
        principal_id="subject-1",
    )
    validator.validate(success)

    failure = await VectorSearchTool().call({}, principal_id="subject-1")
    validator.validate(failure)


@pytest.mark.asyncio
async def test_server_enforces_vector_result_budget() -> None:
    from harborrag_mcp_server.audit import McpAuditLog
    from harborrag_mcp_server.policy import McpToolPolicy

    harbor, _ = runtime([_result("vec-1", "one", 0.95)])
    server = McpServer(
        tools=[VectorSearchTool(runtime=harbor)],
        policy=McpToolPolicy(max_results=0),
        audit=McpAuditLog(),
    )

    with pytest.raises(ValueError, match="MCP result budget exceeded"):
        await server.call_tool(
            "vector_search",
            {"query": "over budget", "tenant_id": "demo"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [{"category": "runbook"}, {"document_title": "x"}, {"fields.Skill Set": "x"}],
)
async def test_vector_search_refuses_unindexed_filter_keys_before_searching(filters) -> None:
    harbor, retrieval = runtime([_result("vec-1", "one", 0.95)])

    result = await VectorSearchTool(runtime=harbor).call(
        {"query": "x", "tenant_id": "demo", "filters": filters},
        principal_id="subject-1",
    )

    assert result["ok"] is False
    assert "unsupported evidence filter" in result["error"]
    assert retrieval.last_request is None


def test_vector_search_filters_schema_lists_indexed_keys() -> None:
    filters = VectorSearchTool.spec.input_schema["properties"]["filters"]

    keys = filters["propertyNames"]["anyOf"][0]["enum"]

    assert {"issue_key", "status", "assignee", "labels"} <= set(keys)
    assert "fields.<key>" in filters["description"]


@pytest.mark.asyncio
async def test_a_threshold_on_the_sparse_lane_is_refused() -> None:
    result = await VectorSearchTool(runtime=runtime([])[0]).call(
        {"query": "x", "tenant_id": "demo", "lane": "sparse", "score_threshold": 0.5},
        principal_id="subject-1",
    )

    assert result["ok"] is False
    assert "sparse" in result["error"]


@pytest.mark.asyncio
async def test_a_threshold_drops_hits_whose_similarity_was_never_measured() -> None:
    harbor, _ = runtime([_result("measured", "a", 0.6, 0.6), _result("unmeasured", "b", 0.99)])

    result = await VectorSearchTool(runtime=harbor).call(
        {"query": "x", "tenant_id": "demo", "score_threshold": 0.5},
        principal_id="subject-1",
    )

    assert [item["id"] for item in result["results"]] == ["measured"]


@pytest.mark.asyncio
async def test_a_refused_filter_reports_its_reason_not_a_backend_failure() -> None:
    from harborrag_core.contracts.errors import HarborValidationError

    class RefusingRetrievalFacade:
        async def search(self, request):
            raise HarborValidationError("source field filter(s) fields.skill_set have no index")

    result = await VectorSearchTool(
        runtime=SimpleNamespace(retrieval=RefusingRetrievalFacade())
    ).call(
        {"query": "x", "tenant_id": "demo", "filters": {"fields.skill_set": "Java"}},
        principal_id="subject-1",
    )

    assert result == {
        "ok": False,
        "error": "source field filter(s) fields.skill_set have no index",
    }


@pytest.mark.asyncio
async def test_hits_carry_the_issue_key_but_not_constant_bookkeeping() -> None:
    hit = _result("vec-1", "Senior Java Engineer", 0.9, 0.5)
    hit.metadata.update({"issue_key": "CPM-101361", "content_hash": "abc", "raw_score": 0.4})
    harbor, _ = runtime([hit])

    result = await VectorSearchTool(runtime=harbor).call(
        {"query": "java", "tenant_id": "demo"}, principal_id="subject-1"
    )

    metadata = result["results"][0]["metadata"]
    assert metadata["issue_key"] == "CPM-101361"
    assert {
        "record_kind",
        "retrieval_source",
        "raw_score",
        "quality_score",
        "content_hash",
    }.isdisjoint(metadata)
    # Everything citations and the Explorer read is still there.
    assert {"document_id", "document_title", "section_path", "citation_locator"} <= set(metadata)
