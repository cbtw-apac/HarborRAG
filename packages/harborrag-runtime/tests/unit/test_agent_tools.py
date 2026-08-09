"""Tests for runtime-native retrieval tools used by the agent engine."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_runtime.agent.tools import RuntimeAgentToolProvider
from harborrag_runtime.contracts import RetrievalResponse
from harborrag_runtime.sdk import RetrievalLane


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

    assert invalid == {"ok": False, "error": "top_k must be between 1 and 20"}
    assert unknown == {"ok": False, "error": "agent tool is not available"}


@pytest.mark.asyncio
async def test_agent_tool_backend_failure_returns_generic_error_but_logs_the_cause(
    caplog,
) -> None:
    class _RaisingRetrieval:
        async def search(self, request):
            raise RuntimeError("vector store unreachable")

    provider = RuntimeAgentToolProvider(_Runtime(_RaisingRetrieval()))  # type: ignore[arg-type]

    with caplog.at_level("ERROR", logger="harborrag.runtime.agent.tools"):
        response = await provider.call_tool(
            "vector_search",
            {"tenant_id": "ACME", "query": "question"},
        )

    assert response == {"ok": False, "error": "agent retrieval tool failed"}
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
        "graph_neighborhood",
    }
    assert {tool.capability for tool in tools} == {"read"}
