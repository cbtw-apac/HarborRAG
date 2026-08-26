from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from harborrag_core.base import StrictModel, utc_now
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId, TenantId
from harborrag_core.security.context import AccessContext


class StorageFamily(StrEnum):
    """Logical persistence families selected independently at runtime."""

    DATABASE = "database"
    VECTOR = "vector"
    GRAPH = "graph"
    STATE = "state"
    CACHE = "cache"
    OBJECT_STORE = "object_store"


class HealthStatus(StrEnum):
    """Enumerates supported health status values."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class StorageOperationContext(StrictModel):
    """Small durable context required to enforce and replay storage operations."""

    access: AccessContext
    operation_kind: str = Field(default="unspecified", min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=512)
    document_id: DocumentId | None = None
    document_version_id: DocumentVersionId | None = None

    @classmethod
    def system(
        cls,
        tenant_id: str | TenantId,
        *,
        operation_kind: str = "unspecified",
        idempotency_key: str | None = None,
        document_id: DocumentId | None = None,
        document_version_id: DocumentVersionId | None = None,
    ) -> StorageOperationContext:
        """Create storage context for trusted background execution."""

        return cls(
            access=AccessContext.system(tenant_id),
            operation_kind=operation_kind,
            idempotency_key=idempotency_key,
            document_id=document_id,
            document_version_id=document_version_id,
        )

    @classmethod
    def for_access(
        cls,
        access: AccessContext,
        *,
        operation_kind: str = "unspecified",
        idempotency_key: str | None = None,
        document_id: DocumentId | None = None,
        document_version_id: DocumentVersionId | None = None,
    ) -> StorageOperationContext:
        """Create a context without replacing the authenticated principal."""

        return cls(
            access=access,
            operation_kind=operation_kind,
            idempotency_key=idempotency_key,
            document_id=document_id,
            document_version_id=document_version_id,
        )

    @property
    def tenant_id(self) -> TenantId:
        """Expose the enforced tenant to repository implementations."""

        return self.access.tenant_id


class RepositoryHealth(StrictModel):
    """Provides a sanitized health snapshot for one configured backend."""

    family: StorageFamily
    backend: str
    instance_name: str
    status: HealthStatus
    checked_at: datetime = Field(default_factory=utc_now)
    latency_ms: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
