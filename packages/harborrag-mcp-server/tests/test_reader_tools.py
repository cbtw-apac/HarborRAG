from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from catalog_support import EXPECTED_READER_TOOLS

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
    ReadableSource,
)
from harborrag_mcp_server.server import McpServer, create_mcp_server
from harborrag_runtime.reader_contracts import (
    DOCUMENT_CONTEXT_LIMIT,
    EVIDENCE_BATCH_LIMIT,
    SOURCE_LIST_LIMIT,
    DocumentListResponse,
    DocumentMetadata,
    DocumentMetadataResponse,
)
from harborrag_runtime.sdk import (
    DocumentContextResponse,
    EvidenceReadItem,
    EvidenceReadResponse,
    GraphNodeResolveResponse,
    RetrievalLane,
    RetrievalResponse,
    SourceListResponse,
)
from harborrag_runtime.tools.reader_catalog import (
    FETCH_EVIDENCE_SPEC,
    GET_DOCUMENT_CONTEXT_SPEC,
    LIST_SOURCES_SPEC,
)


def _node(key: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.GITHUB_REPOSITORY,
        logical_id=f"provider-{key}",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id="source-1",
        title="Payments",
    )


@dataclass
class FakeReaderKnowledge:
    evidence_requests: list[object] = field(default_factory=list)
    context_requests: list[object] = field(default_factory=list)
    source_requests: list[object] = field(default_factory=list)
    node_requests: list[object] = field(default_factory=list)
    document_requests: list[object] = field(default_factory=list)

    async def list_documents(self, request):
        self.document_requests.append(request)
        page = 2 if request.after_document_id else 1
        return DocumentListResponse(
            f"documents-{page}",
            (DocumentMetadata(f"document-{page}", "version-1", "Guide", "source-1", "local", 2),),
            "document-1" if page == 1 else None,
        )

    async def get_document_metadata(self, request):
        self.document_requests.append(request)
        return DocumentMetadataResponse(
            "document-1",
            DocumentMetadata("document-1", "version-1", "Guide", "source-1", "local", 2)
            if request.document_id == "document-1"
            else None,
        )

    async def read_evidence(self, request):
        self.evidence_requests.append(request)
        items = tuple(
            EvidenceReadItem(
                item.chunk_id,
                "available" if item.chunk_id == "chunk-1" else "unavailable",
                text="Canonical evidence" if item.chunk_id == "chunk-1" else None,
                document_id="document-1" if item.chunk_id == "chunk-1" else None,
                document_version_id="version-1" if item.chunk_id == "chunk-1" else None,
                source_scope_id="source-1" if item.chunk_id == "chunk-1" else None,
                connector_type="local" if item.chunk_id == "chunk-1" else None,
                chunk_kind="text" if item.chunk_id == "chunk-1" else None,
                ordinal=0 if item.chunk_id == "chunk-1" else None,
                section_path=("Overview",) if item.chunk_id == "chunk-1" else (),
                citation_locator={"start_line": 1} if item.chunk_id == "chunk-1" else {},
            )
            for item in request.items
        )
        return EvidenceReadResponse("evidence-1", items)

    async def get_document_context(self, request):
        self.context_requests.append(request)
        offset = request.offset
        return DocumentContextResponse(
            request_id=f"context-{offset}",
            outcome="ok",
            document_id=request.document_id,
            document_version_id="version-1",
            chunks=(_context_chunk(f"chunk-{offset + 1}", offset),),
            outline=(("Overview",),) if request.include_outline else (),
            next_offset=offset + 1 if offset == 0 else None,
        )

    async def list_sources(self, request):
        self.source_requests.append(request)
        page = 2 if request.after_source_scope_id else 1
        value = ReadableSource(
            source_scope_id=f"source-{page}",
            connector_type="local",
            display_name=f"Local source {page}",
            ingestion_state="COMPLETED",
            last_source_check_at=datetime(2026, 9, page, tzinfo=UTC),
            active_document_count=page,
        )
        return SourceListResponse(f"sources-{page}", (value,), has_more=page == 1)

    async def resolve_graph_nodes(self, request):
        self.node_requests.append(request)
        return GraphNodeResolveResponse("nodes-1", (_node("node-1"), _node("node-2")))


def test_reader_transport_bounds_share_the_runtime_contract() -> None:
    evidence = FETCH_EVIDENCE_SPEC.input_schema["properties"]["items"]
    context = GET_DOCUMENT_CONTEXT_SPEC.input_schema["properties"]["limit"]
    sources = LIST_SOURCES_SPEC.input_schema["properties"]["limit"]

    assert evidence["maxItems"] == EVIDENCE_BATCH_LIMIT
    assert context["maximum"] == context["default"] == DOCUMENT_CONTEXT_LIMIT
    assert sources["maximum"] == sources["default"] == SOURCE_LIST_LIMIT


def _context_chunk(chunk_id: str, ordinal: int):
    from harborrag_runtime.contracts import DocumentContextChunk

    return DocumentContextChunk(
        chunk_id,
        ordinal,
        f"Context {ordinal}",
        "text",
        ("Overview",),
        {"start_line": ordinal + 1},
    )


@dataclass
class FakeRuntime:
    knowledge: FakeReaderKnowledge = field(default_factory=FakeReaderKnowledge)


@pytest.mark.asyncio
async def test_default_catalog_contains_all_shared_reader_and_graph_tools() -> None:
    server = McpServer(runtime=FakeRuntime())  # type: ignore[arg-type]
    assert [spec.name for spec in server.list_tools()] == EXPECTED_READER_TOOLS
    assert server.list_tools()[4].input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


@pytest.mark.asyncio
async def test_document_inventory_cursor_is_bound_to_tenant_and_principal() -> None:
    runtime = FakeRuntime()
    server = McpServer(runtime=runtime)  # type: ignore[arg-type]
    first = await server.call_tool(
        "list_documents", {"tenant_id": "tenant-1", "limit": 1}, principal_id="reader-1"
    )
    assert first["documents"][0]["document_id"] == "document-1"
    assert first["completion"]["complete"] is False
    cursor = first["next_cursor"]
    for tenant, principal in (("tenant-2", "reader-1"), ("tenant-1", "reader-2")):
        denied = await server.call_tool(
            "list_documents", {"tenant_id": tenant, "cursor": cursor}, principal_id=principal
        )
        assert denied["ok"] is False
    second = await server.call_tool(
        "list_documents", {"tenant_id": "tenant-1", "cursor": cursor}, principal_id="reader-1"
    )
    assert second["documents"][0]["document_id"] == "document-2"
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_document_metadata_returns_unavailable_without_leaking_metadata() -> None:
    server = McpServer(runtime=FakeRuntime())  # type: ignore[arg-type]
    visible = await server.call_tool(
        "get_document_metadata", {"tenant_id": "tenant-1", "document_id": "document-1"}
    )
    assert visible["document"]["title"] == "Guide"
    hidden = await server.call_tool(
        "get_document_metadata", {"tenant_id": "tenant-1", "document_id": "hidden"}
    )
    assert hidden["document"] is None
    assert hidden["completion"] == {"complete": False, "reasons": ["unavailable"]}


@pytest.mark.asyncio
async def test_verify_citations_checks_content_digest_and_reports_unavailable_evidence() -> None:
    server = McpServer(runtime=FakeRuntime())  # type: ignore[arg-type]
    digest = hashlib.sha256(b"Canonical evidence").hexdigest()
    result = await server.call_tool(
        "verify_citations",
        {
            "tenant_id": "tenant-1",
            "items": [
                {"chunk_id": "chunk-1", "expected_content_sha256": digest},
                {"chunk_id": "hidden"},
            ],
        },
    )
    assert result["items"] == [
        {"chunk_id": "chunk-1", "valid": True, "content_sha256": digest},
        {"chunk_id": "hidden", "valid": False, "content_sha256": None},
    ]
    mismatch = await server.call_tool(
        "verify_citations",
        {
            "tenant_id": "tenant-1",
            "items": [{"chunk_id": "chunk-1", "expected_content_sha256": "0" * 64}],
        },
    )
    assert mismatch["items"][0]["valid"] is False
    assert mismatch["completion"]["complete"] is False


@pytest.mark.asyncio
async def test_composed_search_uses_semantic_mode_then_returns_canonical_content() -> None:
    class Retrieval:
        request = None

        async def search(self, request):
            self.request = request
            return RetrievalResponse(
                "search-1",
                RetrievalLane.HYBRID,
                (
                    RetrievalResult(
                        "chunk-1",
                        "Index content",
                        0.9,
                        {"document_id": "document-1", "document_version_id": "version-1"},
                    ),
                ),
                {},
            )

    @dataclass
    class Runtime(FakeRuntime):
        retrieval: Retrieval = field(default_factory=Retrieval)

    runtime = Runtime()
    result = await McpServer(runtime=runtime).call_tool(  # type: ignore[arg-type]
        "composed_evidence_search",
        {"tenant_id": "tenant-1", "query": "guide"},
        principal_id="reader-1",
    )
    assert result["ok"] is True
    assert result["items"][0]["text"] == "Canonical evidence"
    assert runtime.retrieval.request.mode.value == "local_semantic"
    assert (
        runtime.knowledge.evidence_requests[0].items[0].expected_document_version_id == "version-1"
    )
    assert result["cost"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_fetch_evidence_preserves_mixed_batch_availability_and_expectations() -> None:
    runtime = FakeRuntime()
    result = await McpServer(runtime=runtime).call_tool(  # type: ignore[arg-type]
        "fetch_evidence",
        {
            "tenant_id": "tenant-1",
            "items": [
                {
                    "chunk_id": "chunk-1",
                    "expected_document_id": "document-1",
                    "expected_document_version_id": "version-1",
                },
                {"chunk_id": "hidden-or-missing"},
            ],
        },
        principal_id="reader-1",
    )

    assert [item["availability"] for item in result["items"]] == [
        "available",
        "unavailable",
    ]
    assert result["items"][0]["text"] == "Canonical evidence"
    assert result["items"][1]["document_id"] is None
    assert result["completion"] == {
        "complete": False,
        "reasons": ["unavailable"],
    }
    request = runtime.knowledge.evidence_requests[0]
    assert request.items[0].expected_document_version_id == "version-1"


@pytest.mark.asyncio
async def test_document_cursor_is_owner_bound_and_keeps_one_version() -> None:
    runtime = FakeRuntime()
    server = McpServer(runtime=runtime)  # type: ignore[arg-type]
    first = await server.call_tool(
        "get_document_context",
        {
            "tenant_id": "tenant-1",
            "document_id": "document-1",
            "limit": 1,
            "include_outline": True,
        },
        principal_id="reader-1",
    )
    second = await server.call_tool(
        "get_document_context",
        {
            "tenant_id": "tenant-1",
            "document_id": "document-1",
            "limit": 1,
            "include_outline": True,
            "cursor": first["next_cursor"],
        },
        principal_id="reader-1",
    )
    denied = await server.call_tool(
        "get_document_context",
        {
            "tenant_id": "tenant-1",
            "document_id": "document-1",
            "cursor": first["next_cursor"],
        },
        principal_id="reader-2",
    )

    assert [item["chunk_id"] for item in first["chunks"]] == ["chunk-1"]
    assert [item["chunk_id"] for item in second["chunks"]] == ["chunk-2"]
    assert runtime.knowledge.context_requests[1].expected_document_version_id == "version-1"
    assert denied == {"ok": False, "error": "cursor is unavailable"}


@pytest.mark.asyncio
async def test_source_pagination_and_graph_resolution_are_explicit() -> None:
    runtime = FakeRuntime()
    server = McpServer(runtime=runtime)  # type: ignore[arg-type]
    sources = await server.call_tool(
        "list_sources", {"tenant_id": "tenant-1", "limit": 1}, principal_id="reader-1"
    )
    next_sources = await server.call_tool(
        "list_sources",
        {"tenant_id": "tenant-1", "limit": 1, "cursor": sources["next_cursor"]},
        principal_id="reader-1",
    )
    resolved = await server.call_tool(
        "resolve_graph_nodes",
        {
            "tenant_id": "tenant-1",
            "selector": {"kind": "exact_title", "value": "Payments"},
            "source_ids": ["source-1"],
        },
        principal_id="reader-1",
    )

    assert [item["source_id"] for item in sources["sources"]] == ["source-1"]
    assert [item["source_id"] for item in next_sources["sources"]] == ["source-2"]
    assert resolved["resolution"] == "ambiguous"
    assert [item["node_key"] for item in resolved["candidates"]] == ["node-1", "node-2"]
    assert all(item["content_availability"] == "unknown" for item in resolved["candidates"])


@pytest.mark.asyncio
async def test_new_reader_tool_runs_over_real_fastmcp_transport() -> None:
    fastmcp = pytest.importorskip("fastmcp")
    registry = McpServer(runtime=FakeRuntime())  # type: ignore[arg-type]
    transport = create_mcp_server(registry=registry, allow_unauthenticated_local=True)
    async with fastmcp.Client(transport) as client:
        result = await client.call_tool(
            "fetch_evidence",
            {"tenant_id": "tenant-1", "items": [{"chunk_id": "chunk-1"}]},
        )

    assert result.is_error is False
    assert result.structured_content["items"][0]["availability"] == "available"


@pytest.mark.asyncio
async def test_tool_reported_failure_sets_mcp_error_flag() -> None:
    fastmcp = pytest.importorskip("fastmcp")
    transport = create_mcp_server(registry=McpServer(), allow_unauthenticated_local=True)
    async with fastmcp.Client(transport) as client:
        result = await client.call_tool(
            "fetch_evidence",
            {"tenant_id": "tenant-1", "items": [{"chunk_id": "chunk-1"}]},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "reader backend is not configured" in result.content[0].text
