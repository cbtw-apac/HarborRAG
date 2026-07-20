from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from harborrag_adapters.repositories.database.sqlalchemy import (
    SQLChunkRepository,
    SQLDocumentRepository,
)
from harborrag_adapters.repositories.telemetry import RepositoryTelemetry
from harborrag_core.schemas.documents import DocumentRecord, DocumentStatus
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext


class FakeMappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeMappings:
        return FakeMappings(self._rows)


class PagedSession:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self._pages = pages
        self.limits: list[int | None] = []

    async def execute(self, statement: Any) -> FakeResult:
        limit_clause = statement._limit_clause
        self.limits.append(limit_clause.value if limit_clause is not None else None)
        return FakeResult(self._pages.pop(0) if self._pages else [])


def document_row(index: int, *, vectorized: bool) -> dict[str, Any]:
    record = DocumentRecord(
        id=f"doc-{index:03d}",
        tenant_id="tenant-a",
        current_version_id="v1",
        content_hash=f"hash-{index}",
        status=DocumentStatus.READY,
        metadata={"vector_collections": ["docs"]} if vectorized else {},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
    )
    return record.model_dump(mode="json")


@pytest.mark.asyncio
async def test_ready_without_vectors_scans_in_bounded_batches() -> None:
    rows = [document_row(index, vectorized=index < 250) for index in range(251)]
    session = PagedSession([rows[:100], rows[100:200], rows[200:]])
    telemetry = RepositoryTelemetry(None, family=StorageFamily.DATABASE, backend="sqlite")
    repository = SQLDocumentRepository(session, "sqlite", "test", telemetry)

    result = await repository.list_ready_without_vectors(
        "docs",
        limit=1,
        context=StorageOperationContext(tenant_id="tenant-a"),
    )

    assert [record.id for record in result] == ["doc-250"]
    assert session.limits == [100, 100, 100]


@pytest.mark.asyncio
async def test_ready_without_vectors_returns_empty_list_for_non_positive_limit() -> None:
    telemetry = RepositoryTelemetry(None, family=StorageFamily.DATABASE, backend="sqlite")
    repository = SQLDocumentRepository(None, "sqlite", "test", telemetry)

    result = await repository.list_ready_without_vectors(
        "docs", limit=0, context=StorageOperationContext(tenant_id="tenant-a")
    )

    assert result == []


@pytest.mark.asyncio
async def test_ready_without_vectors_stops_when_a_page_returns_no_rows() -> None:
    session = PagedSession([[]])
    telemetry = RepositoryTelemetry(None, family=StorageFamily.DATABASE, backend="sqlite")
    repository = SQLDocumentRepository(session, "sqlite", "test", telemetry)

    result = await repository.list_ready_without_vectors(
        "docs", limit=5, context=StorageOperationContext(tenant_id="tenant-a")
    )

    assert result == []
    assert session.limits == [100]


@pytest.mark.asyncio
async def test_chunk_bulk_upsert_returns_early_for_empty_records() -> None:
    telemetry = RepositoryTelemetry(None, family=StorageFamily.DATABASE, backend="sqlite")
    repository = SQLChunkRepository(None, "sqlite", "test", telemetry)

    # A session of None would blow up if bulk_upsert touched it; reaching the end of
    # this call without error proves the empty-list early return was taken.
    await repository.bulk_upsert([], context=StorageOperationContext(tenant_id="tenant-a"))


@pytest.mark.asyncio
async def test_chunk_get_many_returns_early_for_empty_ids() -> None:
    telemetry = RepositoryTelemetry(None, family=StorageFamily.DATABASE, backend="sqlite")
    repository = SQLChunkRepository(None, "sqlite", "test", telemetry)

    result = await repository.get_many([], context=StorageOperationContext(tenant_id="tenant-a"))

    assert result == []
