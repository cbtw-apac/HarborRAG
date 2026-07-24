"""SQLAlchemy table schemas and row-to-domain mappings."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime
from harborrag_core.schemas.documents import ChunkRecord, DocumentRecord

METADATA = MetaData()

DOCUMENTS = Table(
    "harbor_documents",
    METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("data_source_id", String(64), nullable=True),
    Column("current_version_id", String(64), nullable=False),
    Column("external_id", Text, nullable=True),
    Column("title", Text, nullable=True),
    Column("media_type", String(255), nullable=True),
    Column("content_hash", String(128), nullable=False),
    Column("object_uri", Text, nullable=True),
    Column("status", String(32), nullable=False),
    Column("metadata", JSON, nullable=False, default=dict),
    Column("version", Integer, nullable=False, default=1),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("deleted_at", UTCDateTime(), nullable=True),
)

CHUNKS = Table(
    "harbor_chunks",
    METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("document_id", String(64), nullable=False, index=True),
    Column("document_version_id", String(64), nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("content_hash", String(128), nullable=False),
    Column("token_count", Integer, nullable=True),
    Column("metadata", JSON, nullable=False, default=dict),
    Column("created_at", UTCDateTime(), nullable=False),
)

OUTBOX = Table(
    "harbor_outbox",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", String(64), nullable=False, index=True),
    Column("event_type", String(255), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
)

VECTOR_COLLECTIONS_KEY = "vector_collections"
CANONICAL_CHUNK_KEY = "_harborrag_chunk"
CANONICAL_CHUNK_FIELDS = (
    "logical_chunk_id",
    "chunk_revision_id",
    "artifact_id",
    "artifact_revision_id",
    "ordinal",
    "role",
    "structural_path",
    "start_offset",
    "end_offset",
    "page_start",
    "page_end",
    "start_line",
    "end_line",
    "parent_chunk_id",
    "previous_chunk_id",
    "next_chunk_id",
    "source_element_ids",
    "source_span",
    "context",
)


def document_from_row(row: Any) -> DocumentRecord:
    return DocumentRecord.model_validate(
        {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "data_source_id": row["data_source_id"],
            "current_version_id": row["current_version_id"],
            "external_id": row["external_id"],
            "title": row["title"],
            "media_type": row["media_type"],
            "content_hash": row["content_hash"],
            "object_uri": row["object_uri"],
            "status": row["status"],
            "metadata": dict(row["metadata"] or {}),
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }
    )


def chunk_from_row(row: Any) -> ChunkRecord:
    metadata = dict(row["metadata"] or {})
    canonical = metadata.pop(CANONICAL_CHUNK_KEY, {})
    canonical_fields = (
        {key: canonical[key] for key in CANONICAL_CHUNK_FIELDS if key in canonical}
        if isinstance(canonical, dict)
        else {}
    )
    return ChunkRecord.model_validate(
        {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "document_id": row["document_id"],
            "document_version_id": row["document_version_id"],
            "chunk_index": row["chunk_index"],
            "content": row["content"],
            "content_hash": row["content_hash"],
            "token_count": row["token_count"],
            "metadata": metadata,
            "created_at": row["created_at"],
            **canonical_fields,
        }
    )


def chunk_metadata(record: ChunkRecord) -> dict[str, Any]:
    serialized = record.model_dump(mode="json")
    metadata = dict(serialized["metadata"])
    metadata[CANONICAL_CHUNK_KEY] = {
        "logical_chunk_id": str(record.logical_chunk_id),
        "chunk_revision_id": str(record.chunk_revision_id),
        "artifact_id": record.artifact_id,
        "artifact_revision_id": record.artifact_revision_id,
        "ordinal": record.ordinal,
        "role": record.role,
        "structural_path": list(record.structural_path),
        "start_offset": record.start_offset,
        "end_offset": record.end_offset,
        "page_start": record.page_start,
        "page_end": record.page_end,
        "start_line": record.start_line,
        "end_line": record.end_line,
        "parent_chunk_id": (
            str(record.parent_chunk_id) if record.parent_chunk_id is not None else None
        ),
        "previous_chunk_id": (
            str(record.previous_chunk_id)
            if record.previous_chunk_id is not None
            else None
        ),
        "next_chunk_id": (
            str(record.next_chunk_id) if record.next_chunk_id is not None else None
        ),
        "source_element_ids": list(record.source_element_ids),
        "source_span": serialized["source_span"],
        "context": serialized["context"],
    }
    return metadata
