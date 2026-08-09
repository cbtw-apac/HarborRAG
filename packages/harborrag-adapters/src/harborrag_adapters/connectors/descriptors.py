from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import AdmissionSnapshot


@dataclass(frozen=True, slots=True)
class ConnectorDocumentDescriptor:
    """Compact admission data and independently dispatchable bound records."""

    source: SourceRecord
    admission: AdmissionSnapshot
    bound_records: tuple[SourceRecord, ...] = ()

    def __post_init__(self) -> None:
        bound_ids = [record.id for record in self.bound_records]
        if len(set(bound_ids)) != len(bound_ids):
            raise ValueError("connector bound-record identities must be unique")
        if self.source.id in bound_ids:
            raise ValueError("a source record cannot be bound to itself")


def default_document_descriptor(
    source: SourceRecord,
) -> ConnectorDocumentDescriptor:
    """Describe a connector that has no independently ingestible children."""

    return ConnectorDocumentDescriptor(
        source=source,
        admission=AdmissionSnapshot(
            source_version=source_version(source),
        ),
    )


def source_version(source: SourceRecord) -> str:
    """Resolve an inexpensive stable version from a discovered source record."""

    metadata = source.metadata
    candidates = (
        metadata.get("source_version"),
        metadata.get("version"),
        source.checksum,
        metadata.get("checksum"),
        source.updated_at.isoformat() if source.updated_at is not None else None,
        metadata.get("updated_at"),
    )
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    payload = json.dumps(
        {
            "id": source.id,
            "source_type": source.source_type,
            "locator": source.locator,
            "metadata": source.metadata,
        },
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
