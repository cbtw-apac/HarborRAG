from __future__ import annotations

from bootstrap import probe_suffix, require_healthy

from harborrag_adapters.repositories.database.base import HarborDatabaseBackend
from harborrag_core.schemas.documents import ChunkRecord, DocumentRecord, DocumentStatus
from harborrag_core.schemas.ids import DocumentId
from harborrag_core.schemas.storage import StorageOperationContext


async def exercise_database(
    backend: HarborDatabaseBackend,
    *,
    commit: bool,
) -> tuple[StorageOperationContext, DocumentId]:
    """Exercise health, document, chunk, and outbox operations."""

    require_healthy(await backend.health())
    factory = backend.unit_of_work_factory
    if factory is None:
        raise RuntimeError("database backend did not provide a unit-of-work factory")

    suffix = probe_suffix()
    context = StorageOperationContext(tenant_id=f"smoke-{suffix}")
    document = DocumentRecord(
        id=f"document-{suffix}",
        tenant_id=context.tenant_id,
        current_version_id=f"version-{suffix}",
        title="HarborRAG repository smoke probe",
        content_hash=f"hash-{suffix}",
        status=DocumentStatus.READY,
        metadata={"smoke_test": True},
    )
    chunk = ChunkRecord.from_legacy(
        logical_chunk_id=f"chunk-{suffix}",
        chunk_revision_id=f"chunk-{suffix}",
        tenant_id=context.tenant_id,
        document_id=document.id,
        document_version_id=document.current_version_id,
        artifact_id=str(document.id),
        artifact_revision_id=str(document.current_version_id),
        ordinal=0,
        role="content",
        content="repository smoke probe",
        content_hash=f"chunk-hash-{suffix}",
        token_count=3,
    )

    async with factory() as unit_of_work:
        await unit_of_work.documents.create(document, context=context)
        loaded_document = await unit_of_work.documents.get(document.id, context=context)
        if loaded_document is None or loaded_document.content_hash != document.content_hash:
            raise AssertionError("document did not round-trip inside the transaction")

        await unit_of_work.chunks.bulk_upsert([chunk], context=context)
        loaded_chunks = await unit_of_work.chunks.get_many(
            [str(chunk.chunk_revision_id)],
            context=context,
        )
        if [item.chunk_revision_id for item in loaded_chunks] != [chunk.chunk_revision_id]:
            raise AssertionError("chunk did not round-trip inside the transaction")

        await unit_of_work.outbox.add(
            "smoke.probed",
            {"document_id": str(document.id)},
            context=context,
        )
        if commit:
            await unit_of_work.commit()
        else:
            await unit_of_work.rollback()

    return context, document.id
