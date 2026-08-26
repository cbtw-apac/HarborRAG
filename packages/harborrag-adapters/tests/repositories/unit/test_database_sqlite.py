from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.sqlite.config import SQLiteDatabaseConfig
from harborrag_adapters.repositories.database.sqlite.repository import (
    SQLiteDatabaseBackend,
)
from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
)
from harborrag_core.chunking import (
    ChunkHierarchy,
    ChunkKind,
    ChunkRecord,
    ChunkSecurity,
    CitationLocator,
    ConnectorType,
    DocumentKind,
    RecordKind,
)
from harborrag_core.schemas.documents import DocumentRecord, DocumentStatus
from harborrag_core.schemas.storage import HealthStatus, StorageOperationContext


def make_context(tenant: str = "tenant-a") -> StorageOperationContext:
    return StorageOperationContext.system(tenant_id=tenant)


def make_backend(tmp_path: Path) -> SQLiteDatabaseBackend:
    return SQLiteDatabaseBackend(
        SQLiteDatabaseConfig(database=str(tmp_path / "harbor.db"), create_schema=True)
    )


def make_chunk(tenant_id: str, index: int) -> ChunkRecord:
    content = f"chunk {index}"
    return ChunkRecord(
        strategy_version="strategy-1",
        chunk_id=f"chunk:{index}",
        logical_chunk_id=f"logical-chunk:{index}",
        content_hash=f"hash-{index}",
        connector_type=ConnectorType.LOCAL,
        document_kind=DocumentKind.LOCAL_FILE,
        record_kind=RecordKind.EVIDENCE,
        chunk_kind=ChunkKind.TABLE if index == 1 else ChunkKind.TEXT,
        tenant_id=tenant_id,
        connection_id="connection-1",
        source_scope_id="scope-1",
        source_item_id="guide.md",
        source_version="source-version-1",
        document_id="doc-1",
        document_version_id="v1",
        ordinal=index,
        content=content,
        embedding_text=f"Section {index}\n\n{content}",
        search_text=f"Section {index} {content}",
        token_count=2,
        citation_locator=CitationLocator(source_element_ids=(f"element-{index}",)),
        hierarchy=ChunkHierarchy(section_path=("Section", str(index))),
        security=ChunkSecurity(permission_set_id="permission-set:public"),
        table_locator=(
            {
                "table_id": "table:1",
                "table_version_id": "table-version:1",
                "row_start": 1,
                "row_end": 1,
                "column_count": 1,
                "selected_column_indices": (0,),
                "selected_columns": ("Value",),
            }
            if index == 1
            else None
        ),
    )


@pytest.mark.asyncio
async def test_document_create_get_save_round_trip(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        record = DocumentRecord(
            id="doc-1", tenant_id=context.tenant_id, current_version_id="v1", content_hash="abc"
        )
        async with backend.unit_of_work_factory() as uow:
            await uow.documents.create(record, context=context)
            await uow.commit()

        async with backend.unit_of_work_factory() as uow:
            loaded = await uow.documents.get("doc-1", context=context)
            assert loaded is not None
            assert loaded.content_hash == "abc"

            updated = loaded.model_copy(update={"content_hash": "def"})
            saved = await uow.documents.save(
                updated, expected_version=loaded.version, context=context
            )
            assert saved.version == loaded.version + 1
            await uow.commit()

        async with backend.unit_of_work_factory() as uow:
            reloaded = await uow.documents.get("doc-1", context=context)
            assert reloaded.content_hash == "def"


@pytest.mark.asyncio
async def test_document_create_rejects_duplicate_id(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        record = DocumentRecord(
            id="doc-1",
            tenant_id=context.tenant_id,
            current_version_id="v1",
            content_hash="abc",
        )
        async with backend.unit_of_work_factory() as uow:
            await uow.documents.create(record, context=context)
            await uow.commit()

        with pytest.raises(HarborStorageAlreadyExistsError):
            async with backend.unit_of_work_factory() as uow:
                await uow.documents.create(record, context=context)
                await uow.commit()


@pytest.mark.asyncio
async def test_document_save_detects_version_conflict(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        record = DocumentRecord(
            id="doc-1",
            tenant_id=context.tenant_id,
            current_version_id="v1",
            content_hash="abc",
        )
        async with backend.unit_of_work_factory() as uow:
            await uow.documents.create(record, context=context)
            await uow.commit()

        with pytest.raises(HarborStorageCheckpointConflictError):
            async with backend.unit_of_work_factory() as uow:
                await uow.documents.save(record, expected_version=99, context=context)
                await uow.commit()


@pytest.mark.asyncio
async def test_uncommitted_unit_of_work_rolls_back(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        record = DocumentRecord(
            id="doc-1",
            tenant_id=context.tenant_id,
            current_version_id="v1",
            content_hash="abc",
        )
        async with backend.unit_of_work_factory() as uow:
            await uow.documents.create(record, context=context)
            # Deliberately not calling uow.commit().

        async with backend.unit_of_work_factory() as uow:
            assert await uow.documents.get("doc-1", context=context) is None


@pytest.mark.asyncio
async def test_chunks_bulk_upsert_and_list_by_document(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        chunks = [make_chunk(str(context.tenant_id), index) for index in range(3)]
        async with backend.unit_of_work_factory() as uow:
            await uow.chunks.bulk_upsert(chunks, context=context)
            await uow.outbox.add("chunk.upserted", {"count": 3}, context=context)
            await uow.commit()

        async with backend.unit_of_work_factory() as uow:
            loaded = await uow.chunks.list_by_document("doc-1", context=context)
            assert [chunk.chunk_id for chunk in loaded] == [
                "chunk:0",
                "chunk:1",
                "chunk:2",
            ]
            assert [chunk.logical_chunk_id for chunk in loaded] == [
                "logical-chunk:0",
                "logical-chunk:1",
                "logical-chunk:2",
            ]
            assert loaded[1].chunk_kind == ChunkKind.TABLE
            assert loaded[2].hierarchy.section_path == ("Section", "2")

        async with backend.unit_of_work_factory() as uow:
            fetched = await uow.chunks.get_many(["chunk:0", "chunk:2", "missing"], context=context)
            assert sorted(chunk.chunk_id for chunk in fetched) == [
                "chunk:0",
                "chunk:2",
            ]


@pytest.mark.asyncio
async def test_list_ready_without_vectors_filters_by_status_and_collection(
    tmp_path: Path,
) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        ready = DocumentRecord(
            id="doc-1",
            tenant_id=context.tenant_id,
            current_version_id="v1",
            content_hash="a",
            status=DocumentStatus.READY,
        )
        pending = DocumentRecord(
            id="doc-2",
            tenant_id=context.tenant_id,
            current_version_id="v1",
            content_hash="b",
            status=DocumentStatus.PENDING,
        )
        already_vectorized = DocumentRecord(
            id="doc-3",
            tenant_id=context.tenant_id,
            current_version_id="v1",
            content_hash="c",
            status=DocumentStatus.READY,
            metadata={"vector_collections": ["docs"]},
        )
        async with backend.unit_of_work_factory() as uow:
            await uow.documents.create(ready, context=context)
            await uow.documents.create(pending, context=context)
            await uow.documents.create(already_vectorized, context=context)
            await uow.commit()

        async with backend.unit_of_work_factory() as uow:
            results = await uow.documents.list_ready_without_vectors(
                "docs", limit=10, context=context
            )
            assert [item.id for item in results] == ["doc-1"]


@pytest.mark.asyncio
async def test_explicit_rollback_discards_uncommitted_writes(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        record = DocumentRecord(
            id="doc-1",
            tenant_id=context.tenant_id,
            current_version_id="v1",
            content_hash="abc",
        )
        async with backend.unit_of_work_factory() as uow:
            await uow.documents.create(record, context=context)
            await uow.rollback()

        async with backend.unit_of_work_factory() as uow:
            assert await uow.documents.get("doc-1", context=context) is None


@pytest.mark.asyncio
async def test_connect_skips_schema_creation_when_not_requested(tmp_path: Path) -> None:
    config = SQLiteDatabaseConfig(database=str(tmp_path / "harbor.db"), create_schema=False)
    backend = SQLiteDatabaseBackend(config)
    async with backend:
        pass  # Reaching here without error proves the create_schema branch was skipped.


@pytest.mark.asyncio
async def test_health_reports_healthy_status_once_connected(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        health = await backend.health()
    assert health.status == HealthStatus.HEALTHY
    assert health.backend == "sqlite"
