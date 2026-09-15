"""Safe reader-facing source catalog contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from harborrag_core.base import StrictModel
from harborrag_core.security import AccessContext


class ReadableSource(StrictModel):
    """One corpus scope a principal may read, without connection configuration."""

    source_scope_id: str = Field(min_length=1, max_length=128)
    connector_type: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=256)
    ingestion_state: str | None = Field(default=None, max_length=32)
    last_source_check_at: datetime | None = None
    last_successful_source_check_at: datetime | None = None
    last_successful_ingestion_at: datetime | None = None
    active_document_count: int = Field(default=0, ge=0)


class SourceCatalogQuery(StrictModel):
    """Bounded source page selector passed through the runtime port."""

    tenant_id: str = Field(min_length=1, max_length=128)
    access: AccessContext
    source_scope_ids: tuple[str, ...] = Field(default=(), max_length=20)
    connector_types: tuple[str, ...] = Field(default=(), max_length=10)
    after_source_scope_id: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=20, ge=1, le=21)


__all__ = ["ReadableSource", "SourceCatalogQuery"]
