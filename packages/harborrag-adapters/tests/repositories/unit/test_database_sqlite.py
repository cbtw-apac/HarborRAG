from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.sqlalchemy.schemas import (
    chunk_from_row,
    chunk_metadata,
)
from harborrag_adapters.repositories.database.sqlite.config import SQLiteDatabaseConfig
from harborrag_adapters.repositories.database.sqlite.repository import (
    SQLiteDatabaseBackend,
)
from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
)
from harborrag_core.schemas.documents import (
    ChunkContext,
    ChunkRecord,
    ChunkSourceSpan,
    DocumentRecord,
    DocumentStatus,
)
from harborrag_core.schemas.storage import HealthStatus, StorageOperationContext


def make_context(tenant: str = "tenant-a") -> StorageOperationContext:
    return StorageOperationContext(tenant_id=tenant)


def make_backend(tmp_path: Path) -> SQLiteDatabaseBackend:
    return SQLiteDatabaseBackend(
        SQLiteDatabaseConfig(database=str(tmp_path / "harbor.db"), create_schema=True)
    )


def test_canonical_chunk_fields_round_trip_through_storage_metadata() -> None:
    record = ChunkRecord(
        logical_chunk_id="logical-1",
        chunk_revision_id="revision-1",
        tenant_id="tenant-a",
        document_id="doc-1",
        document_version_id="doc-version-1",
        artifact_id="artifact-1",
        artifact_revision_id="artifact-version-1",
        ordinal=0,
        role="section",
        content="content",
        content_hash="hash",
        token_count=1,
        source_span=ChunkSourceSpan(
            start_offset=8,
            end_offset=15,
            start_line=4,
            end_line=5,
            source_element_ids=("element-1",),
        ),
        context=ChunkContext(
            title="HarborRAG",
            structural_path=("Guide",),
        ),
        metadata={"source": "parser", "nested": {"values": [1, 2]}},
    )

    storage_metadata = chunk_metadata(record)
    storage_metadata["_harborrag_chunk"]["chunk_revision_id"] = "untrusted-override"
    loaded = chunk_from_row(
        {
            "id": str(record.chunk_revision_id),
            "tenant_id": str(record.tenant_id),
            "document_id": str(record.document_id),
            "document_version_id": str(record.document_version_id),
            "chunk_index": record.ordinal,
            "content": record.content,
            "content_hash": record.content_hash,
            "token_count": record.token_count,
            "metadata": storage_metadata,
            "created_at": record.created_at,
        }
    )

    assert loaded == record


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
        chunks = [
            ChunkRecord(
                logical_chunk_id=f"logical-{i}",
                chunk_revision_id=f"c{i}",
                tenant_id=context.tenant_id,
                document_id="doc-1",
                document_version_id="v1",
                artifact_id="artifact-1",
                artifact_revision_id="artifact-revision-1",
                ordinal=i,
                role="table" if i == 1 else "content",
                content=f"chunk {i}",
                content_hash=f"hash{i}",
                source_span=ChunkSourceSpan(
                    source_element_ids=(f"element-{i}",),
                ),
                context=ChunkContext(structural_path=("Section", str(i))),
            )
            for i in range(3)
        ]
        async with backend.unit_of_work_factory() as uow:
            await uow.chunks.bulk_upsert(chunks, context=context)
            await uow.outbox.add("chunk.upserted", {"count": 3}, context=context)
            await uow.commit()

        async with backend.unit_of_work_factory() as uow:
            loaded = await uow.chunks.list_by_document("doc-1", context=context)
            assert [chunk.chunk_revision_id for chunk in loaded] == [
                "c0",
                "c1",
                "c2",
            ]
            assert [chunk.logical_chunk_id for chunk in loaded] == [
                "logical-0",
                "logical-1",
                "logical-2",
            ]
            assert loaded[1].role == "table"
            assert loaded[2].context.structural_path == ("Section", "2")

        async with backend.unit_of_work_factory() as uow:
            fetched = await uow.chunks.get_many(["c0", "c2", "missing"], context=context)
            assert sorted(chunk.chunk_revision_id for chunk in fetched) == [
                "c0",
                "c2",
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
