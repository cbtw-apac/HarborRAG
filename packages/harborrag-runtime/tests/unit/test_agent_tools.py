"""Tests for runtime-native retrieval tools used by the agent engine."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_engine.agent.execution import ChatAndToolExecutor
from harborrag_engine.agent.schemas import AgentRunOptions
from harborrag_runtime.agent.memory_tool_specs import MEMORY_AGENT_TOOL_SPECS
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


def _options() -> AgentRunOptions:
    return AgentRunOptions(tenant_id="ACME", principal_id="svc-1", session_id="session-1")


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


@pytest.mark.asyncio
async def test_describe_graph_stays_callable_through_the_agent_loop() -> None:
    """A tenant-free tool must not be handed a tenant it cannot accept.

    ``describe_graph`` is the one catalog tool with no ``tenant_id`` property,
    and it tells the model to call it with no arguments. While the engine bound
    every call to a tenant unconditionally, the resulting key failed the tool's
    own ``additionalProperties: false`` and the tool could never run.
    """

    provider = RuntimeAgentToolProvider(_Runtime(_Retrieval()))  # type: ignore[arg-type]
    executor = ChatAndToolExecutor(object(), provider, memory=None)  # type: ignore[arg-type]
    specs = {spec.name: spec for spec in executor.available_specs("ACME", graph_search=True)}

    scoped = executor._scoped_arguments(specs["describe_graph"], {}, _options())
    response = await provider.call_tool("describe_graph", scoped)

    assert scoped == {}
    assert response["ok"] is True


@pytest.mark.asyncio
async def test_a_schema_rejection_never_echoes_the_models_own_arguments() -> None:
    """The rejection has to explain the constraint, not quote the payload.

    jsonschema renders a combinator failure as ``repr(instance)``. Forwarding
    that verbatim handed back up to the whole argument budget, and the engine
    then truncated the explanation off the end -- leaving the model with
    kilobytes of its own request and no diagnosis.
    """

    provider = RuntimeAgentToolProvider(_Runtime(_Retrieval()))  # type: ignore[arg-type]
    padding = "x" * 60_000

    response = await provider.call_tool(
        "vector_search",
        {"tenant_id": "ACME", "query": "q", "filters": {"tenant_id": "ACME", "note": padding}},
    )

    error = str(response["error"])
    assert response["ok"] is False
    assert padding not in error
    assert len(error) < 500
    assert "filters" in error


_TENANT_FREE_TOOLS = frozenset({"describe_graph", "search_memory", "manage_memory"})
"""Tools that legitimately take no tenant, each for a stated reason.

``describe_graph`` reads only the static graph contract and touches no tenant's
data. The two memory tools are bound server-side to a full ``MemoryOwner`` that
already carries the tenant, and their schemas forbid owner fields outright.
"""


def test_every_tool_either_declares_a_tenant_or_is_knowingly_tenant_free() -> None:
    """The engine binds a tenant only where the schema declares the property.

    That makes an omitted ``tenant_id`` property a silent grant of an unscoped
    call rather than a loud failure: ``ToolSpec.input_schema`` defaults to a
    bare ``{"type": "object"}``, so a tool added without a schema would receive
    the model's raw arguments with no tenant bound and nothing would complain.
    Keep the exemptions explicit so that day is a test failure.
    """

    specs = [tool.spec for tool in build_reader_tool_catalog(None, KnowledgeReferenceStore())]
    specs.extend(MEMORY_AGENT_TOOL_SPECS)
    assert specs, "the catalog must not be empty"

    unscoped = {
        spec.name
        for spec in specs
        if "tenant_id" not in (spec.input_schema.get("properties") or {})
    }

    assert unscoped == _TENANT_FREE_TOOLS


def test_a_tenant_free_tool_forbids_the_properties_it_does_not_declare() -> None:
    """Their safety rests on the schema refusing anything extra."""

    specs = {spec.name: spec for spec in MEMORY_AGENT_TOOL_SPECS}
    specs["describe_graph"] = next(
        tool.spec
        for tool in build_reader_tool_catalog(None, KnowledgeReferenceStore())
        if tool.spec.name == "describe_graph"
    )

    for name in _TENANT_FREE_TOOLS:
        assert specs[name].input_schema.get("additionalProperties") is False, name
