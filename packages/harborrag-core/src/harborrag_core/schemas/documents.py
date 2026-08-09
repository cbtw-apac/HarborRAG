from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from harborrag_core.base import StrictModel, utc_now
from harborrag_core.schemas.ids import DataSourceId, DocumentId, DocumentVersionId, TenantId


class DocumentStatus(StrEnum):
    """Enumerates supported document status values."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class DocumentRecord(StrictModel):
    """Represents document record data shared across HarborRAG layers."""

    id: DocumentId
    tenant_id: TenantId
    data_source_id: DataSourceId | None = None
    current_version_id: DocumentVersionId
    external_id: str | None = None
    title: str | None = None
    media_type: str | None = None
    content_hash: str
    object_uri: str | None = None
    status: DocumentStatus = DocumentStatus.PENDING
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None
