"""Reader tools turn bad input and backend failures into explicit tool replies."""

from __future__ import annotations

import json

import pytest

from harborrag_core.contracts.errors import HarborCapabilityError, HarborDeadlineExceeded
from harborrag_core.contracts.reader import (
    DocumentContextChunk,
    DocumentContextResponse,
    EvidenceReadItem,
    EvidenceReadResponse,
    GraphNodeResolveResponse,
)
from harborrag_core.contracts.tools import ToolInvocationContext
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.security import AccessContext
from harborrag_engine.tools import reader_tools
from harborrag_engine.tools.reader_tools import (
    FetchEvidenceTool,
    GetDocumentContextTool,
    ResolveGraphNodesTool,
)

TENANT = "tenant-1"
PRINCIPAL = "reader-1"


def _node(key: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.GITHUB_REPOSITORY,
        logical_id=f"provider-{key}",
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id=TENANT,
        source_scope_id="source-1",
        title="Payments",
    )


class _Knowledge:
    """Answers every reader call with a configured response or error."""

    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[object] = []

    async def _answer(self, request: object) -> object:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response

    async def read_evidence(self, request: object) -> object:
        return await self._answer(request)

    async def get_document_context(self, request: object) -> object:
        return await self._answer(request)

    async def resolve_graph_nodes(self, request: object) -> object:
        return await self._answer(request)


def _mismatched_context() -> ToolInvocationContext:
    return ToolInvocationContext(
        access=AccessContext(principal_id="someone-else", tenant_id=TENANT)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_cls", "arguments"),
    [
        (FetchEvidenceTool, {"tenant_id": TENANT, "items": [{"chunk_id": "chunk-1"}]}),
        (GetDocumentContextTool, {"tenant_id": TENANT, "document_id": "document-1"}),
        (
            ResolveGraphNodesTool,
            {"tenant_id": TENANT, "selector": {"kind": "exact_title", "value": "Payments"}},
        ),
    ],
)
async def test_reader_tools_refuse_a_principal_other_than_the_invocation_identity(
    tool_cls: type[FetchEvidenceTool | GetDocumentContextTool | ResolveGraphNodesTool],
    arguments: dict[str, object],
) -> None:
    knowledge = _Knowledge()

    with pytest.raises(PermissionError, match="tool principal does not match"):
        await tool_cls(knowledge=knowledge).call(  # type: ignore[arg-type]
            arguments, principal_id=PRINCIPAL, context=_mismatched_context()
        )

    assert knowledge.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"tenant_id": TENANT, "items": "chunk-1"}, "items must be an array"),
        (
            {"tenant_id": TENANT, "items": [{"chunk_id": " "}]},
            "chunk_id must be a non-empty string",
        ),
        ({"tenant_id": TENANT, "items": ["chunk-1"]}, "each evidence item must be an object"),
        (
            {"tenant_id": TENANT, "items": [{"chunk_id": "a"}, {"chunk_id": "a"}]},
            "evidence read chunk IDs must be unique",
        ),
    ],
)
async def test_fetch_evidence_reports_invalid_items(
    arguments: dict[str, object], error: str
) -> None:
    knowledge = _Knowledge()

    result = await FetchEvidenceTool(knowledge=knowledge).call(  # type: ignore[arg-type]
        arguments, principal_id=PRINCIPAL
    )

    assert result == {"ok": False, "error": error}
    assert knowledge.requests == []


@pytest.mark.asyncio
async def test_fetch_evidence_reports_complete_when_every_item_is_available() -> None:
    knowledge = _Knowledge(
        EvidenceReadResponse("evidence-1", (EvidenceReadItem("chunk-1", "available", "Text"),))
    )

    result = await FetchEvidenceTool(knowledge=knowledge).call(  # type: ignore[arg-type]
        {"tenant_id": TENANT, "items": [{"chunk_id": "chunk-1"}]}, principal_id=PRINCIPAL
    )

    assert result["ok"] is True
    assert result["items"][0]["text"] == "Text"  # type: ignore[index]
    assert result["completion"] == {"complete": True, "reasons": []}


@pytest.mark.asyncio
async def test_fetch_evidence_without_a_backend_names_the_missing_capability() -> None:
    result = await FetchEvidenceTool().call(
        {"tenant_id": TENANT, "items": [{"chunk_id": "chunk-1"}]}, principal_id=PRINCIPAL
    )

    assert result == {"ok": False, "error": "knowledge reader backend is not configured"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_cls", "arguments", "message"),
    [
        (
            FetchEvidenceTool,
            {"tenant_id": TENANT, "items": [{"chunk_id": "chunk-1"}]},
            "evidence retrieval failed",
        ),
        (
            GetDocumentContextTool,
            {"tenant_id": TENANT, "document_id": "document-1"},
            "document context retrieval failed",
        ),
        (
            ResolveGraphNodesTool,
            {"tenant_id": TENANT, "selector": {"kind": "exact_title", "value": "Payments"}},
            "graph node resolution failed",
        ),
    ],
)
async def test_unexpected_backend_errors_are_logged_and_hidden(
    tool_cls: type[FetchEvidenceTool | GetDocumentContextTool | ResolveGraphNodesTool],
    arguments: dict[str, object],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge = _Knowledge(error=RuntimeError("secret backend detail"))
    logged: list[str] = []
    monkeypatch.setattr(reader_tools.logger, "exception", logged.append)

    result = await tool_cls(knowledge=knowledge).call(  # type: ignore[arg-type]
        arguments, principal_id=PRINCIPAL
    )

    assert result == {"ok": False, "error": message}
    assert "secret backend detail" not in json.dumps(result)
    assert len(logged) == 1
    assert logged[0].endswith(" failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_cls", [GetDocumentContextTool, ResolveGraphNodesTool, FetchEvidenceTool]
)
async def test_capability_errors_pass_their_message_through(
    tool_cls: type[FetchEvidenceTool | GetDocumentContextTool | ResolveGraphNodesTool],
) -> None:
    knowledge = _Knowledge(error=HarborCapabilityError("reader is read-only here"))
    arguments = {
        "tenant_id": TENANT,
        "document_id": "document-1",
        "items": [{"chunk_id": "chunk-1"}],
        "selector": {"kind": "exact_title", "value": "Payments"},
    }

    result = await tool_cls(knowledge=knowledge).call(  # type: ignore[arg-type]
        arguments, principal_id=PRINCIPAL
    )

    assert result == {"ok": False, "error": "reader is read-only here"}


def _context_response(
    outcome: str = "ok", next_offset: int | None = None
) -> DocumentContextResponse:
    return DocumentContextResponse(
        request_id="context-1",
        outcome=outcome,  # type: ignore[arg-type]
        document_id="document-1",
        document_version_id="version-2",
        chunks=(DocumentContextChunk("chunk-1", 0, "Body", "text", ("Overview",), {"page": 1}),),
        outline=(("Overview",),),
        next_offset=next_offset,
        document_title="Guide",
    )


@pytest.mark.asyncio
async def test_document_context_reports_a_non_ok_outcome_and_a_window_only_outline() -> None:
    knowledge = _Knowledge(_context_response(outcome="output_limit"))

    result = await GetDocumentContextTool(knowledge=knowledge).call(  # type: ignore[arg-type]
        {"tenant_id": TENANT, "document_id": "document-1", "include_outline": True},
        principal_id=PRINCIPAL,
    )

    assert result["outcome"] == "output_limit"
    assert result["outline_complete"] is False
    assert result["next_cursor"] is None
    assert result["chunks"] == [  # type: ignore[comparison-overlap]
        {
            "chunk_id": "chunk-1",
            "ordinal": 0,
            "text": "Body",
            "chunk_kind": "text",
            "section_path": ["Overview"],
            "citation_locator": {"page": 1},
        }
    ]
    assert result["completion"] == {
        "complete": False,
        "reasons": ["output_limit", "outline_window_only"],
    }


@pytest.mark.asyncio
async def test_document_context_cursor_cannot_be_replayed_against_another_document() -> None:
    knowledge = _Knowledge(_context_response(next_offset=1))
    tool = GetDocumentContextTool(knowledge=knowledge)  # type: ignore[arg-type]
    first = await tool.call(
        {"tenant_id": TENANT, "document_id": "document-1"}, principal_id=PRINCIPAL
    )
    cursor = first["next_cursor"]
    assert isinstance(cursor, str)
    assert first["completion"] == {"complete": False, "reasons": ["page_limit"]}

    other_document = await tool.call(
        {"tenant_id": TENANT, "document_id": "document-2", "cursor": cursor},
        principal_id=PRINCIPAL,
    )
    other_version = await tool.call(
        {
            "tenant_id": TENANT,
            "document_id": "document-1",
            "cursor": cursor,
            "expected_document_version_id": "version-1",
        },
        principal_id=PRINCIPAL,
    )

    assert other_document == {"ok": False, "error": "cursor is unavailable for this document"}
    assert other_version == {
        "ok": False,
        "error": "cursor does not match the requested version",
    }
    assert len(knowledge.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "error"),
    [
        ("node-1", "selector must be an object"),
        ({"kind": "not-a-kind", "value": "x"}, "'not-a-kind' is not a valid GraphNodeSelectorKind"),
    ],
)
async def test_resolve_graph_nodes_reports_an_invalid_selector(
    selector: object, error: str
) -> None:
    knowledge = _Knowledge()

    result = await ResolveGraphNodesTool(knowledge=knowledge).call(  # type: ignore[arg-type]
        {"tenant_id": TENANT, "selector": selector}, principal_id=PRINCIPAL
    )

    assert result == {"ok": False, "error": error}
    assert knowledge.requests == []


@pytest.mark.asyncio
async def test_resolve_graph_nodes_explains_how_to_narrow_a_timed_out_query() -> None:
    knowledge = _Knowledge(error=HarborDeadlineExceeded("deadline"))

    result = await ResolveGraphNodesTool(knowledge=knowledge).call(  # type: ignore[arg-type]
        {"tenant_id": TENANT, "selector": {"kind": "exact_title", "value": "Payments"}},
        principal_id=PRINCIPAL,
    )

    assert result == {
        "ok": False,
        "error": "graph query timed out; narrow with source_ids or entity_types, "
        "or use a node_key or provider_id selector",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "resolution", "completion"),
    [
        (GraphNodeResolveResponse("nodes-1", ()), "no_match", {"complete": True, "reasons": []}),
        (
            GraphNodeResolveResponse("nodes-1", (_node("node-1"),)),
            "unique",
            {"complete": True, "reasons": []},
        ),
        (
            GraphNodeResolveResponse("nodes-1", (_node("node-1"),), truncated=True),
            "ambiguous",
            {"complete": False, "reasons": ["candidate_limit"]},
        ),
    ],
)
async def test_resolve_graph_nodes_classifies_the_candidate_set(
    response: GraphNodeResolveResponse, resolution: str, completion: dict[str, object]
) -> None:
    result = await ResolveGraphNodesTool(knowledge=_Knowledge(response)).call(  # type: ignore[arg-type]
        {"tenant_id": TENANT, "selector": {"kind": "node_key", "value": "node-1"}},
        principal_id=PRINCIPAL,
    )

    assert result["resolution"] == resolution
    assert result["completion"] == completion
