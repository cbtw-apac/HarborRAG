"""The HarborRAG Explorer MCP UI server reuses the audited reader boundary."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from catalog_support import EXPECTED_READER_TOOLS
from test_reader_tools import FakeReaderKnowledge

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import McpConfigurationStore
from harborrag_mcp_server.server import create_explorer_server, create_mcp_server
from harborrag_mcp_server.server.explorer import (
    EXPLORER_ENTRY_TOOL,
    explorer_modes,
    explorer_tool_names,
    views,
)
from harborrag_mcp_server.server.server import McpServer

fastmcp = pytest.importorskip("fastmcp")
pytest.importorskip("prefab_ui")

EXPLORER_TOOLS = [
    "explorer_search",
    "explorer_read",
    "explorer_browse",
    "explorer_graph",
    EXPLORER_ENTRY_TOOL,
]


def _hit(chunk_id: str, text: str, score: float) -> RetrievalResult:
    return RetrievalResult(
        id=chunk_id,
        text=text,
        score=score,
        metadata={
            "document_id": "document-1",
            "document_version_id": "version-1",
            "record_kind": "evidence",
            "chunk_kind": "text",
            "connector_type": "local",
            "citation_locator": {},
            "quality_score": None,
            "retrieval_source": "qdrant-authoritative",
            "document_title": "Operations Guide",
            "section_path": ["Runbooks", "Failover"],
        },
    )


@dataclass
class _Retrieval:
    requests: list[object] = field(default_factory=list)

    async def search(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            request_id="retrieval-1",
            lane=request.lane,
            results=(_hit("chunk-1", "Fail over the primary " * 40, 0.91),),
            diagnostics={
                "candidate_hits": 1,
                "stale_candidates": 0,
                "unpublished_candidates": 0,
                "malformed_candidates": 0,
                "search_window": 1,
                "graph_nodes": 0,
                "graph_relations": 0,
                "graph_truncated": False,
                "duration_ms": 0.0,
                "graph_documents": [],
            },
        )


@dataclass
class _Runtime:
    retrieval: _Retrieval = field(default_factory=_Retrieval)
    knowledge: FakeReaderKnowledge = field(default_factory=FakeReaderKnowledge)


def _registry(runtime: _Runtime | None = None) -> McpServer:
    return McpServer(runtime=runtime or _Runtime(), audit=McpAuditLog())  # type: ignore[arg-type]


def _explorer(registry: McpServer):
    return create_explorer_server(registry=registry, allow_unauthenticated_local=True)


def _reader_token(tenants: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        claims={"sub": "reader-1", "role": "reader", "tenants": tenants},
        client_id="client",
        scopes=["mcp:read"],
    )


def _audited(registry: McpServer, tool: str) -> list[dict[str, object]]:
    return [entry for entry in registry.audit.entries if entry["tool"] == tool]


@pytest.mark.asyncio
async def test_the_reader_server_keeps_only_the_reader_catalog() -> None:
    transport = create_mcp_server(registry=_registry(), allow_unauthenticated_local=True)
    async with fastmcp.Client(transport) as client:
        names = [tool.name for tool in await client.list_tools()]

    assert names == EXPECTED_READER_TOOLS


@pytest.mark.asyncio
async def test_the_ui_server_serves_only_the_explorer_app() -> None:
    transport = _explorer(_registry())
    async with fastmcp.Client(transport) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        resources = [str(resource.uri) for resource in await client.list_resources()]

    assert list(tools) == EXPLORER_TOOLS
    entry = tools[EXPLORER_ENTRY_TOOL]
    assert entry.meta["ui"]["visibility"] == ["model"]
    assert entry.meta["ui"]["resourceUri"] in resources
    assert entry.annotations is not None
    assert entry.annotations.read_only_hint is True
    for name in EXPLORER_TOOLS[:-1]:
        assert tools[name].meta["ui"]["visibility"] == ["app"]
    assert tools["explorer_search"].input_schema["properties"]["mode"]["enum"] == [
        "evidence",
        "entities",
    ]
    assert "open_explorer" in (transport.instructions or "")


def test_the_ui_server_requires_authentication_like_the_reader_server() -> None:
    with pytest.raises(RuntimeError, match="requires authentication"):
        create_explorer_server(registry=_registry())


@pytest.mark.asyncio
async def test_the_rendered_ui_calls_only_registered_backend_tools() -> None:
    async with fastmcp.Client(_explorer(_registry())) as client:
        registered = {tool.name for tool in await client.list_tools()}
        result = await client.call_tool(EXPLORER_ENTRY_TOOL, {"tenant_id": "tenant-1"})

    payload = json.dumps(result.structured_content)
    referenced = set(re.findall(r'"tool": "([^"]+)"', payload))
    assert referenced == set(EXPLORER_TOOLS[:-1])
    assert referenced <= registered
    state = result.structured_content["state"]
    assert state["tenant_id"] == "tenant-1"
    assert state["tab"] == "search"
    # Evidence reaches the conversation only when the user chooses to send it.
    assert '"action": "sendMessage"' in payload
    assert '"action": "updateContext"' in payload
    assert result.content[0].text


@pytest.mark.asyncio
async def test_opening_with_a_query_runs_the_audited_vector_search() -> None:
    runtime = _Runtime()
    registry = _registry(runtime)
    async with fastmcp.Client(_explorer(registry)) as client:
        result = await client.call_tool(
            EXPLORER_ENTRY_TOOL, {"query": "failover", "tenant_id": "tenant-1"}
        )

    search = result.structured_content["state"]["search"]
    assert search["count"] == 1
    hit = search["hits"][0]
    assert hit["title"] == "Operations Guide"
    assert hit["section"] == "Runbooks / Failover"
    assert hit["evidence_id"] == "chunk-1"
    assert len(hit["snippet"]) <= views.SNIPPET_CHARACTERS
    assert "evidence chunk-1" in search["chat_message"]
    assert runtime.retrieval.requests, "the explorer must reach the reader runtime"
    audited = _audited(registry, "vector_search")
    assert [entry["event"] for entry in audited] == [
        "tool_invocation_attempted",
        "tool_invocation_completed",
    ]
    assert audited[-1]["tenant_id"] == "tenant-1"


@pytest.mark.asyncio
async def test_search_clamps_top_k_to_the_chosen_modes_ceiling() -> None:
    runtime = _Runtime()
    async with fastmcp.Client(_explorer(_registry(runtime))) as client:
        await client.call_tool(
            "explorer_search", {"query": "failover", "tenant_id": "tenant-1", "top_k": 500}
        )

    assert runtime.retrieval.requests[-1].top_k == 20


@pytest.mark.asyncio
async def test_reading_evidence_fetches_it_and_its_reading_window() -> None:
    runtime = _Runtime()
    registry = _registry(runtime)
    async with fastmcp.Client(_explorer(registry)) as client:
        result = await client.call_tool(
            "explorer_read",
            {"tenant_id": "tenant-1", "evidence_id": "chunk-1", "document_id": "document-1"},
        )

    reader = result.structured_content
    assert reader["kind"] == "evidence"
    assert reader["text"] == "Canonical evidence"
    assert reader["section"] == "Overview"
    assert reader["chunks"], "the reading window around the evidence"
    assert "Cite evidence chunk-1" in reader["chat_message"]
    request = runtime.knowledge.evidence_requests[0].items[0]
    assert request.expected_document_id == "document-1"
    context = runtime.knowledge.context_requests[0]
    assert context.anchor_chunk_id == "chunk-1"
    assert [entry["tool"] for entry in registry.audit.entries][::2] == [
        "fetch_evidence",
        "get_document_context",
    ]


@pytest.mark.asyncio
async def test_unavailable_evidence_is_reported_not_shown() -> None:
    async with fastmcp.Client(_explorer(_registry())) as client:
        result = await client.call_tool(
            "explorer_read",
            {"tenant_id": "tenant-1", "evidence_id": "chunk-9"},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "unavailable" in result.content[0].text


@pytest.mark.asyncio
async def test_documents_page_forward_and_open_in_the_reader() -> None:
    runtime = _Runtime()
    async with fastmcp.Client(_explorer(_registry(runtime))) as client:
        first = await client.call_tool(
            "explorer_browse", {"tenant_id": "tenant-1", "kind": "documents"}
        )
        cursor = first.structured_content["next_cursor"]
        second = await client.call_tool(
            "explorer_browse", {"tenant_id": "tenant-1", "kind": "documents", "cursor": cursor}
        )
        document = await client.call_tool(
            "explorer_read", {"tenant_id": "tenant-1", "document_id": "document-1"}
        )

    assert first.structured_content["rows"][0]["title"] == "Guide"
    assert cursor
    assert second.structured_content["rows"][0]["document_id"] == "document-2"
    reader = document.structured_content
    assert reader["kind"] == "document"
    assert reader["title"] == "Guide"
    assert reader["outline"] == ["Overview"]
    assert runtime.knowledge.context_requests[-1].include_outline is True


@pytest.mark.asyncio
async def test_graph_resolution_preserves_ambiguity() -> None:
    async with fastmcp.Client(_explorer(_registry())) as client:
        result = await client.call_tool(
            "explorer_graph",
            {"tenant_id": "tenant-1", "action": "resolve", "value": "Payments"},
        )

    graph = result.structured_content
    assert [row["node_key"] for row in graph["candidates"]] == ["node-1", "node-2"]
    assert graph["summary"].startswith("ambiguous")


@pytest.mark.asyncio
async def test_a_tenantless_backend_call_needs_a_bound_token() -> None:
    async with fastmcp.Client(_explorer(_registry())) as client:
        result = await client.call_tool(
            "explorer_browse", {"kind": "sources"}, raise_on_error=False
        )

    assert result.is_error is True
    assert "Enter a tenant ID" in result.content[0].text


@pytest.mark.asyncio
async def test_a_bound_token_supplies_its_tenant_and_cannot_widen_it(monkeypatch) -> None:
    dependencies = pytest.importorskip("fastmcp.server.dependencies")
    monkeypatch.setattr(dependencies, "get_access_token", lambda: _reader_token(["tenant-1"]))
    runtime = _Runtime()
    registry = _registry(runtime)
    async with fastmcp.Client(_explorer(registry)) as client:
        listed = await client.call_tool("explorer_browse", {"kind": "sources"})
        refused = await client.call_tool(
            "explorer_browse", {"kind": "sources", "tenant_id": "tenant-2"}, raise_on_error=False
        )

    assert listed.structured_content["rows"][0]["name"] == "Local source 1"
    assert refused.is_error is True
    assert len(runtime.knowledge.source_requests) == 1
    refusals = [
        entry
        for entry in _audited(registry, "list_sources")
        if entry.get("error_type") == "PermissionError"
    ]
    assert refusals, "a refused explorer call must leave an audit record"


def _configured(tmp_path, yaml: str) -> McpServer:
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text("version: 1\ntools:\n" + yaml, encoding="utf-8")
    registry = _registry()
    registry.configuration = McpConfigurationStore.load(
        path=config_path, specs=registry.list_tools(), audit=McpAuditLog()
    )
    return registry


@pytest.mark.asyncio
async def test_the_explorer_follows_the_configured_reader_tools(tmp_path) -> None:
    registry = _configured(
        tmp_path,
        "  vector_search:\n    enabled: false\n  resolve_graph_nodes:\n    enabled: false\n"
        "  graph_subgraph_search:\n    enabled: false\n",
    )
    assert explorer_tool_names(registry) == [*EXPLORER_TOOLS[:3], EXPLORER_ENTRY_TOOL]
    assert list(explorer_modes(registry)["explorer_search"]) == ["entities"]

    async with fastmcp.Client(_explorer(registry)) as client:
        refused = await client.call_tool(
            "explorer_search",
            {"query": "x", "tenant_id": "tenant-1", "mode": "evidence"},
            raise_on_error=False,
        )
    assert refused.is_error is True
    assert "not enabled" in refused.content[0].text


def test_a_server_with_nothing_to_explore_refuses_to_start(tmp_path) -> None:
    disabled = "".join(
        f"  {name}:\n    enabled: false\n"
        for name in (
            "vector_search",
            "find_entities",
            "fetch_evidence",
            "get_document_context",
            "list_documents",
            "list_sources",
            "resolve_graph_nodes",
            "graph_subgraph_search",
        )
    )
    registry = _configured(tmp_path, disabled)
    assert explorer_tool_names(registry) == []
    with pytest.raises(RuntimeError, match="disabled"):
        _explorer(registry)


def test_entities_rows_carry_their_first_evidence() -> None:
    rows = views.entity_rows(
        {
            "matches": [
                {
                    "node_key": "jira:PAY-42",
                    "rank": 1,
                    "score": 0.87654,
                    "description": "Payments outage postmortem",
                    "coverage_mode": "complete",
                    "attributes": [{"name": "status", "values": ["Done"]}],
                    "evidence_chunk_ids": ["chunk-7", "chunk-8"],
                }
            ]
        }
    )

    assert rows == [
        {
            "rank": 1,
            "description": "Payments outage postmortem",
            "attributes": "status: Done",
            "score": 0.877,
            "coverage": "complete",
            "evidence_count": 2,
            "first_evidence_id": "chunk-7",
            "node_key": "jira:PAY-42",
        }
    ]


def test_the_neighborhood_chart_cannot_be_broken_by_titles() -> None:
    view = views.neighborhood(
        "node-1",
        {
            "nodes": [
                {"node_key": "node-1", "title": 'Evil"] --> x[click', "node_kind": "SourceEntity"},
                {"node_key": "node-2", "title": "Runbook", "node_kind": "SourceEntity"},
            ],
            "relations": [
                {
                    "relation_type": "links_to",
                    "source_node_key": "node-1",
                    "target_node_key": "node-2",
                    "origin": "source_declared",
                }
            ],
            "completion": {"complete": False, "reasons": ["max_nodes"]},
        },
    )

    chart = view["chart"].splitlines()
    assert chart[0] == "graph LR"
    assert chart[1] == '  n0["Evil -- x click"]'
    assert "  n0 -->|links_to| n1" in chart
    assert chart[-1] == "  style n0 stroke-width:3px"
    assert view["relations"][0]["target"] == "Runbook"
    assert view["summary"].endswith("(bounded; more exists)")


def test_ui_check_lists_only_the_explorer_tools(tmp_path, monkeypatch, capsys) -> None:
    from harborrag_mcp_server.__main__ import ui_main

    monkeypatch.chdir(tmp_path)
    assert ui_main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out) == EXPLORER_TOOLS
