from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.contracts.errors import HarborCapabilityError, HarborNotFoundError
from harborrag_mcp_server.tools.expand_document import ExpandDocumentTool
from harborrag_runtime.contracts import ExpandDocumentRelation, ExpandDocumentResponse


class StaticRetrievalFacade:
    def __init__(
        self, response: ExpandDocumentResponse | None = None, error: Exception | None = None
    ) -> None:
        self.response = response
        self.error = error
        self.last_request = None

    async def expand_document(self, request):
        self.last_request = request
        if self.error is not None:
            raise self.error
        return self.response


def runtime(response: ExpandDocumentResponse | None = None, error: Exception | None = None):
    retrieval = StaticRetrievalFacade(response, error)
    return SimpleNamespace(retrieval=retrieval), retrieval


def _response() -> ExpandDocumentResponse:
    return ExpandDocumentResponse(
        document_id="document-1",
        document_version_id="version-1",
        title="Release guide",
        content_type="page",
        text="The activity timeout is 30 seconds.",
    )


@pytest.mark.asyncio
async def test_expand_document_returns_the_full_source_document() -> None:
    harbor, retrieval = runtime(_response())

    result = await ExpandDocumentTool(runtime=harbor).call(
        {"document_id": "document-1", "tenant_id": "demo"},
        principal_id="subject-1",
    )

    assert result["ok"] is True
    assert result["document_id"] == "document-1"
    assert result["document_version_id"] == "version-1"
    assert result["text"] == "The activity timeout is 30 seconds."
    request = retrieval.last_request
    assert request.access.principal_id == "subject-1"
    assert request.access.tenant_id == "demo"
    assert request.document_id == "document-1"


@pytest.mark.asyncio
async def test_expand_document_forwards_relations_as_plain_dicts() -> None:
    response = ExpandDocumentResponse(
        document_id="document-1",
        document_version_id="version-1",
        title="Release guide",
        content_type="page",
        text="The activity timeout is 30 seconds.",
        relations=(
            ExpandDocumentRelation(
                predicate="has_attachment",
                target_id="confluence://SPACE/attachment-1",
                target_type="document",
            ),
        ),
    )
    harbor, _ = runtime(response)

    result = await ExpandDocumentTool(runtime=harbor).call(
        {"document_id": "document-1", "tenant_id": "demo"},
        principal_id="subject-1",
    )

    assert result["relations"] == (
        {
            "predicate": "has_attachment",
            "target_id": "confluence://SPACE/attachment-1",
            "target_type": "document",
        },
    )


@pytest.mark.asyncio
async def test_expand_document_reports_document_not_found() -> None:
    harbor, _ = runtime(error=HarborNotFoundError("document not found: document-1"))

    result = await ExpandDocumentTool(runtime=harbor).call(
        {"document_id": "document-1", "tenant_id": "demo"},
        principal_id="subject-1",
    )

    assert result == {"ok": False, "error": "document not found"}


@pytest.mark.asyncio
async def test_expand_document_reports_when_backend_not_configured() -> None:
    harbor, _ = runtime(error=HarborCapabilityError("document expansion is not configured"))

    result = await ExpandDocumentTool(runtime=harbor).call(
        {"document_id": "document-1", "tenant_id": "demo"},
        principal_id="subject-1",
    )

    assert result == {"ok": False, "error": "document store backend is not configured"}


@pytest.mark.asyncio
async def test_expand_document_reports_missing_runtime() -> None:
    result = await ExpandDocumentTool().call(
        {"document_id": "document-1", "tenant_id": "demo"},
        principal_id="subject-1",
    )

    assert result == {"ok": False, "error": "document store backend is not configured"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"document_id": " ", "tenant_id": "demo"},
        {"document_id": "document-1", "tenant_id": " "},
        {"document_id": "document-1"},
    ],
)
async def test_expand_document_rejects_invalid_direct_inputs(arguments) -> None:
    assert (await ExpandDocumentTool().call(arguments, principal_id="subject-1"))["ok"] is False


def test_expand_document_schema_requires_document_id_and_tenant_id() -> None:
    schema = ExpandDocumentTool.spec.input_schema

    assert schema["required"] == ["document_id", "tenant_id"]
    assert {"document_id", "tenant_id"} <= set(schema["properties"])


@pytest.mark.asyncio
async def test_backend_failure_returns_generic_error_but_logs_the_cause(caplog) -> None:
    harbor, _ = runtime(error=RuntimeError("object store credentials invalid: minio\r"))

    with caplog.at_level("ERROR", logger="harborrag.mcp.tools.expand_document"):
        result = await ExpandDocumentTool(runtime=harbor).call(
            {"document_id": "document-1", "tenant_id": "demo"},
            principal_id="subject-1",
        )

    assert result == {"ok": False, "error": "document store backend failed"}
    logged = [record for record in caplog.records if record.exc_info is not None]
    assert logged, "the real exception must be logged even though the caller sees a generic error"
    assert "object store credentials invalid" in str(logged[0].exc_info[1])
