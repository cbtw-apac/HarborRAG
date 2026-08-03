from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic_core import to_jsonable_python

from harborrag_core.domain.document import Document, DocumentBlock, DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.domain.table import TableArtifact

from .identity import reject_runtime_fields

CANONICAL_DOCUMENT_SCHEMA_VERSION = 1


def canonical_document_bytes(document: Document) -> bytes:
    """Serialize a canonical document deterministically without runtime data."""

    value = to_jsonable_python(document)
    reject_runtime_fields(value)
    return json.dumps(
        {
            "schema_version": CANONICAL_DOCUMENT_SCHEMA_VERSION,
            "document": value,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def load_canonical_document(payload: bytes) -> Document:
    """Validate and reconstruct every canonical structural field."""

    envelope = json.loads(payload)
    if envelope.get("schema_version") != CANONICAL_DOCUMENT_SCHEMA_VERSION:
        raise ValueError("unsupported canonical document schema version")
    value = envelope.get("document")
    if not isinstance(value, dict):
        raise ValueError("canonical document payload is invalid")
    reject_runtime_fields(value)
    provenance = value["provenance"]
    return Document(
        id=value["id"],
        title=value["title"],
        content=[DocumentElement(**item) for item in value.get("content", ())],
        content_type=value["content_type"],
        provenance=DocumentProvenance(
            source=provenance["source"],
            record_id=provenance.get("record_id"),
            url=provenance.get("url"),
            author=provenance.get("author"),
            checksum=provenance.get("checksum"),
            permissions=dict(provenance.get("permissions") or {}),
            created_at=_datetime(provenance.get("created_at")),
            updated_at=_datetime(provenance.get("updated_at")),
            tags=list(provenance.get("tags") or ()),
            extra=dict(provenance.get("extra") or {}),
        ),
        relations=[
            DocumentRelation(
                predicate=item["predicate"],
                target_id=item["target_id"],
                target_type=item["target_type"],
                metadata=dict(item.get("metadata") or {}),
            )
            for item in value.get("relations", ())
        ],
        raw=value.get("raw"),
        blocks=tuple(DocumentBlock.model_validate(item) for item in value.get("blocks", ())),
        table_artifacts=tuple(
            TableArtifact.model_validate(item) for item in value.get("table_artifacts", ())
        ),
        body_representation=value.get("body_representation"),
        warnings=tuple(value.get("warnings", ())),
    )


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
