from __future__ import annotations

from dataclasses import dataclass

import pytest

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_mcp_server.server import call_tool, list_tools
from harborrag_mcp_server.server.server import McpServer
from harborrag_mcp_server.tools.vector_search import VectorSearchTool


@dataclass
class StaticPipeline:
    results: list[RetrievalResult]
    last_query: RetrievalQuery | None = None

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        self.last_query = query
        ordered = sorted(self.results, key=lambda item: item.score, reverse=True)
        return ordered[: query.top_k]


def test_vector_search_returns_ranked_results() -> None:
    tool = VectorSearchTool(
        pipeline=StaticPipeline(
            [
                RetrievalResult("vec-1", "HarborRAG vector search result one", 0.95),
                RetrievalResult("vec-2", "HarborRAG vector search result two", 0.82),
            ]
        )
    )

    result = tool.call({"query": "HarborRAG vector", "filters": {"tenant_id": "demo"}})

    assert result["ok"] is True
    assert result["query"] == "HarborRAG vector"
    assert result["score_threshold"] == 0.3
    assert len(result["results"]) > 0


def test_vector_search_respects_top_k() -> None:
    pipeline = StaticPipeline(
        [RetrievalResult(f"id-{i}", f"result {i}", float(10 - i)) for i in range(10)]
    )
    tool = VectorSearchTool(pipeline=pipeline)

    result = tool.call({"query": "result", "top_k": 3, "filters": {"tenant_id": "demo"}})

    assert result["ok"] is True
    assert result["top_k"] == 3
    assert len(result["results"]) == 3


def test_vector_search_forwards_filters_to_pipeline() -> None:
    pipeline = StaticPipeline([RetrievalResult("x", "result", 0.9)])
    tool = VectorSearchTool(pipeline=pipeline)

    result = tool.call({"query": "result", "filters": {"tenant_id": "demo"}})

    assert result["ok"] is True
    assert pipeline.last_query is not None
    assert pipeline.last_query.filters == {"tenant_id": "demo"}


def test_vector_search_score_threshold_filters_results() -> None:
    pipeline = StaticPipeline(
        [
            RetrievalResult("high", "alpha", 0.9),
            RetrievalResult("low", "beta", 0.2),
        ]
    )
    tool = VectorSearchTool(pipeline=pipeline)

    result = tool.call(
        {"query": "alpha", "score_threshold": 0.8, "filters": {"tenant_id": "demo"}}
    )

    assert result["ok"] is True
    assert [item["id"] for item in result["results"]] == ["high"]


def test_vector_search_rejects_invalid_inputs() -> None:
    tool = VectorSearchTool()

    assert tool.call({})["ok"] is False
    assert tool.call({"query": "   "})["ok"] is False
    assert tool.call({"query": "x", "top_k": 0})["ok"] is False
    assert tool.call({"query": "x", "top_k": 21})["ok"] is False
    assert tool.call({"query": "x", "top_k": "bad"})["ok"] is False
    assert tool.call({"query": "x", "filters": "bad"})["ok"] is False
    assert tool.call({"query": "x", "score_threshold": -0.1})["ok"] is False
    assert tool.call({"query": "x", "score_threshold": 1.1})["ok"] is False
    assert tool.call({"query": "x", "filters": {}})["ok"] is False
    assert tool.call({"query": "x", "filters": {"tenant_id": "  "}})["ok"] is False
    assert (
        tool.call({"query": "x", "filters": {"tenant_id": "demo"}})["error"]
        == "vector_search backend is not configured"
    )


def test_vector_search_schema_matches_policy_budget() -> None:
    tool = VectorSearchTool()

    assert tool.spec.name == "vector_search"
    assert tool.spec.input_schema["required"] == ["query", "filters"]
    assert tool.spec.input_schema["properties"]["top_k"]["maximum"] == 20
    assert tool.spec.input_schema["properties"]["filters"]["required"] == ["tenant_id"]


def test_vector_search_is_reachable_via_server_and_facades() -> None:
    server = McpServer()

    names = [spec.name for spec in server.list_tools()]
    assert "vector_search" in names

    payload = {"query": "harbor", "filters": {"tenant_id": "demo"}}
    via_server = server.call_tool("vector_search", payload)
    via_facade = call_tool("vector_search", payload)

    assert via_server["ok"] is False
    assert via_facade["ok"] is False
    assert via_server["error"] == "vector_search backend is not configured"
    assert "vector_search" in [spec["name"] for spec in list_tools()]


def test_vector_search_works_when_server_is_configured() -> None:
    pipeline = StaticPipeline(
        [RetrievalResult("vec-1", "HarborRAG vector search result one", 0.95)]
    )
    server = McpServer(tools=[VectorSearchTool(pipeline=pipeline)])

    result = server.call_tool(
        "vector_search",
        {"query": "HarborRAG", "filters": {"tenant_id": "demo"}},
    )

    assert result["ok"] is True
    assert len(result["results"]) == 1


def test_vector_search_facade_records_audit_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    import harborrag_mcp_server.server.server as server_module
    from harborrag_mcp_server.audit import McpAuditLog

    fresh_audit = McpAuditLog()
    monkeypatch.setattr(server_module, "_default_audit_log", fresh_audit)

    call_tool("vector_search", {"query": "audit facade", "filters": {"tenant_id": "demo"}})

    assert [entry["event"] for entry in fresh_audit.entries] == [
        "tool_invocation_attempted",
        "tool_invocation_completed",
    ]
    assert fresh_audit.entries[0]["tool"] == "vector_search"


def test_vector_search_facade_respects_policy_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import harborrag_mcp_server.server as server_facade
    from harborrag_mcp_server.audit import McpAuditLog
    from harborrag_mcp_server.policy import McpToolPolicy

    server = McpServer(
        tools=[
            VectorSearchTool(
                pipeline=StaticPipeline(
                    [RetrievalResult("vec-1", "HarborRAG vector search result one", 0.95)]
                )
            )
        ],
        policy=McpToolPolicy(max_results=0),
        audit=McpAuditLog(),
    )

    monkeypatch.setattr(server_facade, "McpServer", lambda: server)

    with pytest.raises(ValueError, match="MCP result budget exceeded"):
        call_tool("vector_search", {"query": "over budget", "filters": {"tenant_id": "demo"}})
