from __future__ import annotations

import logging

import pytest

from harborrag_core.contracts.errors import HarborNoIndexedContentError
from harborrag_engine.tools.failures import SERVER_SIDE_ERROR_CODES, ToolErrorCode
from harborrag_engine.tools.vector_search import VectorSearchTool

pytestmark = pytest.mark.unit

ARGUMENTS = {"tenant_id": "tenant-1", "query": "release notes"}


class _Reader:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def search(self, request: object) -> object:
        del request
        raise self._error


@pytest.mark.asyncio
async def test_a_tenant_with_nothing_ingested_is_told_so_and_logged_without_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="harborrag.engine.tools.vector_search")
    error = HarborNoIndexedContentError(
        "No content has been ingested yet, so there is nothing to search"
    )

    result = await VectorSearchTool(reader=_Reader(error)).call(ARGUMENTS, principal_id="reader-1")  # type: ignore[arg-type]

    assert result == {
        "ok": False,
        "error": "No content has been ingested yet, so there is nothing to search",
        "error_code": "no_indexed_content",
    }
    (record,) = caplog.records
    assert (record.levelno, record.exc_info) == (logging.WARNING, None)
    assert record.__dict__["error_type"] == "HarborNoIndexedContentError"


@pytest.mark.asyncio
async def test_a_store_failure_is_a_server_side_backend_failure() -> None:
    result = await VectorSearchTool(reader=_Reader(ConnectionError("qdrant:6334 refused"))).call(  # type: ignore[arg-type]
        ARGUMENTS, principal_id="reader-1"
    )

    assert result == {
        "ok": False,
        "error": "vector retrieval backend failed",
        "error_code": "backend_unavailable",
    }
    assert ToolErrorCode.BACKEND_UNAVAILABLE in SERVER_SIDE_ERROR_CODES
    assert ToolErrorCode.NO_INDEXED_CONTENT not in SERVER_SIDE_ERROR_CODES
