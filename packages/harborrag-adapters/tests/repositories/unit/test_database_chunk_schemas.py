from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_adapters.repositories.database.sqlalchemy.schemas import (
    chunk_from_row,
    chunk_metadata,
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


def _chunk(
    *,
    citation_locator: CitationLocator | None = None,
    hierarchy: ChunkHierarchy | None = None,
    metadata: dict[str, object] | None = None,
) -> ChunkRecord:
    return ChunkRecord(
        strategy_version="strategy-1",
        logical_chunk_id="logical-chunk:1",
        chunk_id="chunk:1",
        connector_type=ConnectorType.LOCAL,
        document_kind=DocumentKind.LOCAL_FILE,
        record_kind=RecordKind.EVIDENCE,
        chunk_kind=ChunkKind.TEXT,
        tenant_id="tenant-a",
        connection_id="connection-1",
        source_scope_id="scope-1",
        source_item_id="guide.md",
        source_version="source-version-1",
        document_id="doc-1",
        document_version_id="doc-version-1",
        ordinal=0,
        content="content",
        embedding_text="Guide\n\ncontent",
        search_text="Guide content",
        content_hash="hash",
        token_count=1,
        citation_locator=citation_locator or CitationLocator(),
        hierarchy=hierarchy or ChunkHierarchy(),
        security=ChunkSecurity(permission_set_id="permission-set:public"),
        metadata=metadata or {},
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
        "created_at": None,
    }


def test_canonical_chunk_fields_round_trip_through_storage_metadata() -> None:
    record = _chunk(
        citation_locator=CitationLocator(
            start_offset=8,
            end_offset=15,
            start_line=4,
            end_line=5,
            source_element_ids=("element-1",),
        ),
        hierarchy=ChunkHierarchy(
            document_title="HarborRAG",
            section_path=("Guide",),
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
    record = _chunk()

    loaded = chunk_from_row(_stored_chunk_row(record, chunk_metadata(record), token_count=None))

    assert loaded.token_count == 0


def test_legacy_chunk_storage_metadata_is_rejected() -> None:
    row_record = _chunk()
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

    with pytest.raises(ValidationError):
        chunk_from_row(
            _stored_chunk_row(row_record, old_metadata, token_count=row_record.token_count)
        )
