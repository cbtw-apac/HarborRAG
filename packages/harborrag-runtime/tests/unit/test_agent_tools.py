"""Tests for runtime-native retrieval tools used by the agent engine."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.agent.tools import RuntimeAgentToolProvider
from harborrag_runtime.contracts import RetrievalResponse
from harborrag_runtime.sdk import RetrievalLane
from harborrag_runtime.tools.budgets import ToolBudget
from harborrag_runtime.tools.catalog_factory import build_reader_tool_catalog
from harborrag_runtime.tools.references import KnowledgeReferenceStore


class _Retrieval:
    def __init__(self) -> None:
        self.request = None

    async def search(self, request):
        self.request = request
        return RetrievalResponse(
            request_id="retrieval-1",
            lane=RetrievalLane.HYBRID,
            results=(RetrievalResult("chunk-1", "evidence", 0.91, {"source": "doc"}),),
            diagnostics={"lane": "hybrid"},
        )


@dataclass
class _Runtime:
    retrieval: _Retrieval
    graph: object | None = None


@pytest.mark.asyncio
async def test_vector_tool_enforces_access_identity_and_returns_evidence() -> None:
    runtime = _Runtime(_Retrieval())
    provider = RuntimeAgentToolProvider(runtime)  # type: ignore[arg-type]

    response = await provider.call_tool(
        "vector_search",
        {
            "tenant_id": "ACME",
            "query": "release owner",
            "top_k": 3,
            "filters": {"document_kind": "policy"},
        },
        principal_id="reader-1",
    )

    assert response["ok"] is True
    assert response["results"] == [
        {
            "id": "chunk-1",
            "text": "evidence",
            "score": 0.91,
            "metadata": {"source": "doc"},
            "relevance": None,
        }
    ]
    assert runtime.retrieval.request.access.principal_id == "reader-1"
    assert str(runtime.retrieval.request.access.tenant_id) == "ACME"
    assert runtime.retrieval.request.filters == {"document_kind": "policy"}


@pytest.mark.asyncio
async def test_agent_tools_reject_invalid_or_unknown_calls() -> None:
    provider = RuntimeAgentToolProvider(_Runtime(_Retrieval()))  # type: ignore[arg-type]

    invalid = await provider.call_tool(
        "vector_search",
        {"tenant_id": "ACME", "query": "question", "top_k": 0},
    )
    unknown = await provider.call_tool("write_index", {"tenant_id": "ACME"})

    # The schema the model was shown is now checked before dispatch, so the
    # rejection names the path, the value and the bound rather than repeating a
    # tool's hand-written sentence. Both reach the model as ordinary tool
    # errors, which is what lets the loop correct itself and continue.
    assert invalid == {
        "ok": False,
        "error": (
            "Agent arguments do not match the tool schema. $.top_k: 0 is less than the minimum of 1"
        ),
    }
    assert unknown == {"ok": False, "error": "agent tool is not available"}


@pytest.mark.asyncio
async def test_agent_tool_backend_failure_returns_generic_error_but_logs_the_cause(
    caplog,
) -> None:
    class _RaisingRetrieval:
        async def search(self, request):
            raise RuntimeError("vector store unreachable")

    provider = RuntimeAgentToolProvider(_Runtime(_RaisingRetrieval()))  # type: ignore[arg-type]

    with caplog.at_level("ERROR", logger="harborrag.runtime.tools.vector_search"):
        response = await provider.call_tool(
            "vector_search",
            {"tenant_id": "ACME", "query": "question"},
        )

    assert response == {"ok": False, "error": "vector retrieval backend failed"}
    logged = [record for record in caplog.records if record.exc_info is not None]
    assert logged, "the real exception must be logged even though the caller sees a generic error"
    assert "vector store unreachable" in str(logged[0].exc_info[1])


def test_agent_tool_catalog_exposes_only_bounded_read_tools() -> None:
    provider = RuntimeAgentToolProvider(_Runtime(_Retrieval()))  # type: ignore[arg-type]

    tools = provider.list_tools("ACME")

    assert {tool.name for tool in tools} == {
        "vector_search",
        "graph_triplet_search",
        "graph_path_search",
        "graph_subgraph_search",
        "fetch_evidence",
        "get_document_context",
        "list_sources",
        "describe_graph",
        "resolve_graph_nodes",
        "list_documents",
        "get_document_metadata",
        "verify_citations",
        "composed_evidence_search",
    }
    assert {tool.capability for tool in tools} == {"read"}
    shared = build_reader_tool_catalog(provider.runtime, KnowledgeReferenceStore())
    assert {tool.name: tool.input_schema for tool in tools} == {
        tool.spec.name: tool.spec.input_schema for tool in shared
    }


@pytest.mark.asyncio
async def test_compact_vector_hits_do_not_return_content_and_report_unknown_retrieval_cost() -> (
    None
):
    provider = RuntimeAgentToolProvider(_Runtime(_Retrieval()))  # type: ignore[arg-type]
    result = await provider.call_tool(
        "vector_search", {"tenant_id": "ACME", "query": "release owner", "include_content": False}
    )
    assert "text" not in result["results"][0]
    assert result["results"][0]["id"] == "chunk-1"
    assert result["cost"]["amount_usd"] is None
    assert result["cost"]["complete"] is False


@pytest.mark.asyncio
async def test_agent_transport_bounds_what_a_tool_result_may_cost_in_context() -> None:
    """The agent applies the same ceilings MCP applies to this same catalog.

    MCP rejects an oversized payload outright. On the agent path there is no
    request to reject: whatever a tool returns is appended to model context and
    paid for, so without a ceiling a single call can swallow the token budget.
    The rejection arrives as an ordinary tool error, which is what lets the loop
    narrow its request instead of dying.
    """

    class _HugeRetrieval:
        async def search(self, request):
            del request
            return RetrievalResponse(
                request_id="retrieval-1",
                lane=RetrievalLane.HYBRID,
                results=tuple(
                    RetrievalResult(f"chunk-{index}", "x" * 4096, 0.9, {}) for index in range(20)
                ),
                diagnostics={"lane": "hybrid"},
            )

    provider = RuntimeAgentToolProvider(
        _Runtime(_HugeRetrieval()),  # type: ignore[arg-type]
        budget=ToolBudget(label="Agent", max_output_bytes=2048),
    )

    response = await provider.call_tool(
        "vector_search", {"tenant_id": "ACME", "query": "everything", "top_k": 20}
    )

    assert response == {"ok": False, "error": "Agent output budget exceeded."}


@pytest.mark.asyncio
async def test_agent_transport_bounds_the_number_of_results() -> None:
    class _ManyRetrieval:
        async def search(self, request):
            del request
            return RetrievalResponse(
                request_id="retrieval-1",
                lane=RetrievalLane.HYBRID,
                results=tuple(
                    RetrievalResult(f"chunk-{index}", "evidence", 0.9, {}) for index in range(8)
                ),
                diagnostics={"lane": "hybrid"},
            )

    provider = RuntimeAgentToolProvider(
        _Runtime(_ManyRetrieval()),  # type: ignore[arg-type]
        budget=ToolBudget(label="Agent", max_results=4),
    )

    response = await provider.call_tool(
        "vector_search", {"tenant_id": "ACME", "query": "everything"}
    )

    assert response == {"ok": False, "error": "Agent result budget exceeded."}
