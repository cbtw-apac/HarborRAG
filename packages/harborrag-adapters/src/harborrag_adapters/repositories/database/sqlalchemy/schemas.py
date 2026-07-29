"""SQLAlchemy table schemas and row-to-domain mappings."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text

from harborrag_adapters.repositories.backends.sqlalchemy import UTCDateTime
from harborrag_core.schemas.documents import (
    ChunkRecord,
    DocumentRecord,
)

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
COLUMN_OWNED_CHUNK_FIELDS = frozenset(
    {
        "chunk_id",
        "tenant_id",
        "document_id",
        "document_version_id",
        "ordinal",
        "content",
        "content_hash",
        "token_count",
        "metadata",
        "created_at",
    }
)


def _stored_chunk_payload_fields(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    if not value.get("schema_version"):
        return value
    return {
        key: field_value
        for key, field_value in value.items()
        if key in ChunkRecord.model_fields and key not in COLUMN_OWNED_CHUNK_FIELDS
    }


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
    stored = _stored_chunk_payload_fields(metadata.pop(CANONICAL_CHUNK_KEY, {}))
    payload = {
        **stored,
        "chunk_id": row["id"],
        "tenant_id": row["tenant_id"],
        "document_id": row["document_id"],
        "document_version_id": row["document_version_id"],
        "ordinal": row["chunk_index"],
        "content": row["content"],
        "content_hash": row["content_hash"],
        "token_count": row["token_count"] or 0,
        "metadata": metadata,
        "created_at": row["created_at"],
    }
    return ChunkRecord.from_legacy_payload(payload)


def chunk_metadata(record: ChunkRecord) -> dict[str, Any]:
    serialized = record.model_dump(mode="json")
    metadata = dict(serialized["metadata"])
    metadata[CANONICAL_CHUNK_KEY] = {
        key: value for key, value in serialized.items() if key not in COLUMN_OWNED_CHUNK_FIELDS
    }
    return metadata
