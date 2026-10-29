"""Removing one connection's documents from every store."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from harborrag_runtime.ingestion.maintenance.cleanup import ProjectionCleanupBatch
from harborrag_runtime.ingestion.maintenance.connection_removal import (
    ConnectionRemovalIncompleteError,
    ConnectionRemovalService,
)


def _service(
    *, cleanup_batches: list[ProjectionCleanupBatch]
) -> tuple[ConnectionRemovalService, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    records = SimpleNamespace(
        active_document_ids=AsyncMock(return_value=["doc-1", "doc-2"]),
        source_scope_ids=AsyncMock(return_value=["scope-a", "scope-b"]),
        purge=AsyncMock(),
    )
    control = SimpleNamespace(
        connection_records=records, publisher=SimpleNamespace(retire_removed=AsyncMock())
    )
    cleanup = SimpleNamespace(run_documents=AsyncMock(side_effect=cleanup_batches))
    graph = SimpleNamespace(delete_source_scope=AsyncMock())
    service = ConnectionRemovalService(
        control=cast(Any, control), cleanup=cast(Any, cleanup), graph=cast(Any, graph)
    )
    return service, control, cleanup, graph


@pytest.mark.asyncio
async def test_removal_retires_cleans_drops_scopes_and_purges_on_request() -> None:
    service, control, cleanup, graph = _service(
        cleanup_batches=[ProjectionCleanupBatch(2, 2, 0, 0), ProjectionCleanupBatch(0, 0, 0, 0)]
    )

    summary = await service.remove(tenant_id="acme", source_key="cpm/tickets", purge=True)

    assert (summary.documents_retired, summary.cleanup_completed, summary.source_scopes) == (
        2,
        2,
        2,
    )
    assert {
        call.kwargs["document_id"] for call in control.publisher.retire_removed.await_args_list
    } == {
        "doc-1",
        "doc-2",
    }
    assert cleanup.run_documents.await_args.kwargs["document_ids"] == ["doc-1", "doc-2"]
    assert [call.args[0] for call in graph.delete_source_scope.await_args_list] == [
        "scope-a",
        "scope-b",
    ]
    control.connection_records.purge.assert_awaited_once_with(
        tenant_id="acme", connection_id="cpm/tickets"
    )


@pytest.mark.asyncio
async def test_a_failed_cleanup_stops_before_the_purge_so_the_removal_can_run_again() -> None:
    service, control, _, graph = _service(cleanup_batches=[ProjectionCleanupBatch(2, 1, 0, 1)])

    with pytest.raises(ConnectionRemovalIncompleteError):
        await service.remove(tenant_id="acme", source_key="cpm/tickets", purge=True)

    graph.delete_source_scope.assert_not_awaited()
    control.connection_records.purge.assert_not_awaited()
