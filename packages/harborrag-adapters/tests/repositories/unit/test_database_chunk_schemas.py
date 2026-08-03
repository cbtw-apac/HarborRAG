from __future__ import annotations

from harborrag_adapters.repositories.database.sqlalchemy.schemas import (
    chunk_from_row,
    chunk_metadata,
)
from harborrag_core.schemas.documents import ChunkContext, ChunkRecord, ChunkSourceSpan


def _legacy_chunk(
    *,
    source_span: ChunkSourceSpan | None = None,
    context: ChunkContext | None = None,
    metadata: dict[str, object] | None = None,
) -> ChunkRecord:
    return ChunkRecord.from_legacy(
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
        source_span=source_span,
        context=context,
        metadata=metadata,
    )


def _stored_chunk_row(
    record: ChunkRecord,
    metadata: dict[str, object],
    *,
    token_count: int | None,
) -> dict[str, object]:
    return {
        "id": str(record.chunk_id),
        "tenant_id": str(record.tenant_id),
        "document_id": str(record.document_id),
        "document_version_id": str(record.document_version_id),
        "chunk_index": record.ordinal,
        "content": record.content,
        "content_hash": record.content_hash,
        "token_count": token_count,
        "metadata": metadata,
        "created_at": record.created_at,
    }


def test_canonical_chunk_fields_round_trip_through_storage_metadata() -> None:
    record = _legacy_chunk(
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
    storage_metadata["_harborrag_chunk"]["chunk_id"] = "untrusted-override"
    storage_metadata["_harborrag_chunk"]["tenant_id"] = "untrusted-tenant"
    loaded = chunk_from_row(
        _stored_chunk_row(record, storage_metadata, token_count=record.token_count)
    )

    assert loaded == record
    assert loaded.chunk_id == record.chunk_id
    assert loaded.tenant_id == record.tenant_id


def test_canonical_chunk_null_storage_token_count_normalizes_to_zero() -> None:
    record = _legacy_chunk()

    loaded = chunk_from_row(_stored_chunk_row(record, chunk_metadata(record), token_count=None))

    assert loaded.token_count == 0


def test_legacy_chunk_storage_metadata_uses_the_core_migration_boundary() -> None:
    row_record = _legacy_chunk()
    old_metadata = {
        "source_kind": "jira",
        "_harborrag_chunk": {
            "logical_chunk_id": "legacy-logical",
            "artifact_id": "legacy-artifact",
            "artifact_revision_id": "legacy-artifact-version",
            "role": "jira.comment",
            "source_span": {"start_offset": 0, "end_offset": 7},
            "context": {"title": "Legacy issue"},
        },
    }

    loaded = chunk_from_row(
        _stored_chunk_row(row_record, old_metadata, token_count=row_record.token_count)
    )

    assert loaded.logical_chunk_id == "legacy-logical"
    assert loaded.artifact_id == "legacy-artifact"
    assert loaded.role == "jira.comment"
    assert loaded.source_locator.start_offset == 0
    assert loaded.hierarchy.document_title == "Legacy issue"
