"""Transport-neutral commands for the public ingestion control plane."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IngestionCreateCommand:
    tenant_id: str
    connection_id: str
    force_reprocess: bool
    public_request: dict[str, object]


@dataclass(frozen=True, slots=True)
class DocumentPageCursor:
    updated_at: str
    document_id: str
