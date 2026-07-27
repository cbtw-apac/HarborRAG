"""Smoke tests for VectorSearchTool following the harborrag-mcp test conventions."""
from __future__ import annotations

import pytest
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_engine.retrieval.mock import MockRetrievalPipeline
from harborrag_mcp.server import call_tool, list_tools
from harborrag_mcp.server.mock import MockMcpServer
from harborrag_mcp.tools.vector_search import VectorSearchTool

# ---------------------------------------------------------------------------
# Unit-level: tool in isolation
# ---------------------------------------------------------------------------


def test_vector_search_returns_ranked_results():
    tool = VectorSearchTool()
    result = tool.call({"query": "HarborRAG vector"})

    assert result["ok"] is True
    assert result["query"] == "HarborRAG vector"
    assert isinstance(result["results"], list)
    assert len(result["results"]) > 0
    first = result["results"][0]
    assert {"id", "text", "score", "metadata"} <= first.keys()


def test_vector_search_respects_top_k():
    pipeline = MockRetrievalPipeline(
        [RetrievalResult(f"id-{i}", f"result {i}", float(10 - i)) for i in range(10)]
    )
    tool = VectorSearchTool(pipeline=pipeline)

    result = tool.call({"query": "result", "top_k": 3})

    assert result["ok"] is True
    assert result["top_k"] == 3
    assert len(result["results"]) == 3


def test_vector_search_results_ordered_by_score_descending():
    tool = VectorSearchTool()
    result = tool.call({"query": "search"})

    scores = [r["score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)


def test_vector_search_empty_query_returns_error():
    tool = VectorSearchTool()
    result = tool.call({"query": ""})

    assert result["ok"] is False
    assert "error" in result


def test_vector_search_missing_query_returns_error():
    tool = VectorSearchTool()
    result = tool.call({})

    assert result["ok"] is False
    assert "error" in result


def test_vector_search_accepts_filters():
    tool = VectorSearchTool()
    result = tool.call({"query": "harbor", "filters": {"project": "demo"}})

    assert result["ok"] is True


def test_vector_search_default_top_k_is_applied():
    tool = VectorSearchTool()
    result = tool.call({"query": "harbor"})

    assert result["top_k"] == 5


def test_vector_search_score_threshold_filters_results():
    # Use a query with no terms in common with the document text so
    # MockRetrievalPipeline's term-overlap boost does not change the scores.
    high_score = RetrievalResult("h", "alpha bravo charlie", 0.9)
    low_score = RetrievalResult("l", "delta echo foxtrot", 0.5)
    pipeline = MockRetrievalPipeline([high_score, low_score])
    tool = VectorSearchTool(pipeline=pipeline)

    result = tool.call({"query": "zzz", "score_threshold": 0.8})

    assert result["ok"] is True
    assert result["score_threshold"] == 0.8
    ids = [r["id"] for r in result["results"]]
    assert "h" in ids
    assert "l" not in ids


def test_vector_search_score_threshold_zero_keeps_all():
    pipeline = MockRetrievalPipeline(
        [RetrievalResult(f"id-{i}", f"doc {i}", float(i) / 10) for i in range(5)]
    )
    tool = VectorSearchTool(pipeline=pipeline)

    with_zero = tool.call({"query": "doc", "score_threshold": 0.0})

    assert len(with_zero["results"]) == 5


def test_vector_search_score_threshold_default_is_applied():
    tool = VectorSearchTool()
    result = tool.call({"query": "harbor"})

    assert result["score_threshold"] == 0.3


def test_vector_search_score_threshold_drops_all_when_too_high():
    # Query has no term overlap with doc text so score stays at 0.3.
    pipeline = MockRetrievalPipeline(
        [RetrievalResult("x", "alpha bravo", 0.3)]
    )
    tool = VectorSearchTool(pipeline=pipeline)

    result = tool.call({"query": "zzz", "score_threshold": 0.9})

    assert result["ok"] is True
    assert result["results"] == []


def test_vector_search_spec_matches_expected_schema():
    tool = VectorSearchTool()

    assert tool.spec.name == "vector_search"
    assert "query" in tool.spec.input_schema["properties"]
    assert "top_k" in tool.spec.input_schema["properties"]
    assert "filters" in tool.spec.input_schema["properties"]
    assert tool.spec.input_schema["required"] == ["query"]


# ---------------------------------------------------------------------------
# Integration-level: tool wired through MockMcpServer
# ---------------------------------------------------------------------------


def test_vector_search_tool_listed_in_mock_server():
    server = MockMcpServer()
    tool_names = [spec.name for spec in server.list_tools()]

    assert "vector_search" in tool_names


def test_vector_search_callable_via_mock_server():
    server = MockMcpServer()
    result = server.call_tool("vector_search", {"query": "HarborRAG"})

    assert result["ok"] is True
    assert len(result["results"]) > 0


def test_vector_search_audited_via_mock_server():
    from harborrag_mcp.audit import McpAuditLog
    from harborrag_mcp.policy import McpToolPolicy

    audit = McpAuditLog()
    server = MockMcpServer(policy=McpToolPolicy(), audit=audit)
    server.call_tool("vector_search", {"query": "audit test"})

    assert {"tool": "vector_search"} in audit.entries


# ---------------------------------------------------------------------------
# Integration-level: tool reachable through module-level call_tool facade
# ---------------------------------------------------------------------------


def test_vector_search_reachable_via_call_tool_facade():
    result = call_tool("vector_search", {"query": "facade test"})

    assert result["ok"] is True


def test_vector_search_listed_via_list_tools_facade():
    names = [t["name"] for t in list_tools()]

    assert "vector_search" in names


def test_vector_search_facade_audit_trail(monkeypatch: pytest.MonkeyPatch):
    import harborrag_mcp.server.mock as mock_server
    from harborrag_mcp.audit import McpAuditLog

    fresh_audit = McpAuditLog()
    monkeypatch.setattr(mock_server, "_default_audit_log", fresh_audit)

    call_tool("vector_search", {"query": "audit facade"})

    assert {"tool": "vector_search"} in fresh_audit.entries


def test_vector_search_facade_respects_policy_budget(monkeypatch: pytest.MonkeyPatch):
    import harborrag_mcp.server.mock as mock_server
    from harborrag_mcp.policy import McpToolPolicy

    monkeypatch.setattr(mock_server, "_default_policy", McpToolPolicy(max_results=0))

    with pytest.raises(ValueError, match="MCP result budget exceeded"):
        call_tool("vector_search", {"query": "over budget"})
