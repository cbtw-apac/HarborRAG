"""Projection cleanup only ever asks for the requesting tenant's jobs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_runtime.ingestion import ProjectionCleanupService


def _service() -> tuple[ProjectionCleanupService, AsyncMock]:
    pending = AsyncMock(return_value=())
    control = SimpleNamespace(reliability=SimpleNamespace(pending_cleanup_jobs=pending))
    service = ProjectionCleanupService(
        control=control,  # type: ignore[arg-type]
        vector_store=SimpleNamespace(),  # type: ignore[arg-type]
        graph_store=SimpleNamespace(),
    )
    return service, pending


@pytest.mark.asyncio
async def test_scope_cleanup_asks_only_for_the_requesting_tenants_jobs() -> None:
    service, pending = _service()

    await service.run_scope(tenant_id="tenant-a", source_scope_id="docs")

    pending.assert_awaited_once()
    assert pending.await_args is not None
    assert pending.await_args.kwargs["tenant_id"] == "tenant-a"
    assert pending.await_args.kwargs["source_scope_id"] == "docs"


@pytest.mark.asyncio
async def test_document_cleanup_asks_only_for_the_requesting_tenants_jobs() -> None:
    service, pending = _service()

    await service.run_documents(tenant_id="tenant-a", document_ids=["document:a"])

    pending.assert_awaited_once()
    assert pending.await_args is not None
    assert pending.await_args.kwargs["tenant_id"] == "tenant-a"
    assert pending.await_args.kwargs["document_ids"] == ["document:a"]
