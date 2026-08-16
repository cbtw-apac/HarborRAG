from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_mcp_server.server.server import McpServer
from harborrag_mcp_server.tools.vector_search import VectorSearchTool
from harborrag_runtime.sdk import RetrievalLane


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
            diagnostics={"candidate_hits": len(ordered)},
        )


def runtime(results: list[RetrievalResult]):
    retrieval = StaticRetrievalFacade(results)
    return SimpleNamespace(retrieval=retrieval), retrieval


@pytest.mark.asyncio
async def test_vector_search_defaults_to_hybrid_without_graph_observation() -> None:
    harbor, retrieval = runtime([RetrievalResult("vec-1", "one", 0.95)])

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
        [RetrievalResult("high", "alpha", 0.9), RetrievalResult("low", "beta", 0.2)]
    )

    result = await VectorSearchTool(runtime=harbor).call(
        {
            "query": "alpha",
            "tenant_id": "demo",
            "lane": "dense",
            "filters": {"category": "runbook"},
            "observe_graph": False,
            "score_threshold": 0.8,
        },
        principal_id="subject-1",
    )

    request = retrieval.last_request
    assert request.lane == RetrievalLane.DENSE
    assert request.filters == {"category": "runbook"}
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
async def test_server_enforces_vector_result_budget() -> None:
    from harborrag_mcp_server.audit import McpAuditLog
    from harborrag_mcp_server.policy import McpToolPolicy

    harbor, _ = runtime([RetrievalResult("vec-1", "one", 0.95)])
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
