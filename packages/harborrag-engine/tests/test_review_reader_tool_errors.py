"""Reader tools keep backend error text, which can quote stored content, from clients."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from harborrag_engine.tools.base import BaseTool
from harborrag_engine.tools.document_tools import ListDocumentsTool
from harborrag_engine.tools.find_entities import FindEntitiesTool
from harborrag_engine.tools.reader_tools import (
    FetchEvidenceTool,
    GetDocumentContextTool,
    ResolveGraphNodesTool,
)
from harborrag_engine.tools.source_list_tool import ListSourcesTool

pytestmark = pytest.mark.unit

TENANT = "tenant-1"
PRINCIPAL = "reader-1"
STORED_TEXT = "Stored passage about the DEMO-7 incident"


class _StoredArtifact(BaseModel):
    ordinal: int


def _corrupt_artifact_error() -> ValidationError:
    """A pydantic error whose text quotes the stored value it could not parse."""
    try:
        _StoredArtifact.model_validate({"ordinal": STORED_TEXT})
    except ValidationError as exc:
        return exc
    raise AssertionError("the stored value unexpectedly validated")


class _Backend:
    """Fails every reader call the way a corrupt stored artifact does."""

    def __init__(self) -> None:
        self.calls = 0

    async def _fail(self, request: object) -> object:
        del request
        self.calls += 1
        raise _corrupt_artifact_error()

    read_evidence = _fail
    get_document_context = _fail
    resolve_graph_nodes = _fail
    list_documents = _fail
    find_entities = _fail
    list_sources = _fail


def _tool(tool_cls: type[BaseTool], backend: _Backend) -> BaseTool:
    if tool_cls is FindEntitiesTool:
        return FindEntitiesTool(retrieval=backend)  # type: ignore[arg-type]
    return tool_cls(knowledge=backend)  # type: ignore[call-arg]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_cls", "arguments"),
    [
        (FetchEvidenceTool, {"items": [{"chunk_id": "chunk-1"}]}),
        (GetDocumentContextTool, {"document_id": "document-1"}),
        (ResolveGraphNodesTool, {"selector": {"kind": "exact_title", "value": "Payments"}}),
        (ListDocumentsTool, {}),
        (FindEntitiesTool, {"query": "payments"}),
        (ListSourcesTool, {}),
    ],
    ids=[
        "fetch_evidence",
        "get_document_context",
        "resolve_graph_nodes",
        "list_documents",
        "find_entities",
        "list_sources",
    ],
)
async def test_reader_tool_backend_validation_error_text_is_not_returned(
    tool_cls: type[BaseTool], arguments: dict[str, object]
) -> None:
    backend = _Backend()

    response = await _tool(tool_cls, backend).call(
        {"tenant_id": TENANT, **arguments}, principal_id=PRINCIPAL
    )

    assert backend.calls == 1
    assert response["ok"] is False
    assert STORED_TEXT not in json.dumps(response)
    assert "input_value" not in json.dumps(response)
