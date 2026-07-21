from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from harborrag_core.base import StrictModel, utc_now
from harborrag_core.schemas.ids import ChunkId, DocumentId, TenantId, WorkflowId


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
    """Represents storage operation context data shared across HarborRAG layers."""

    tenant_id: TenantId
    request_id: str | None = None
    trace_id: str | None = None
    workflow_id: WorkflowId | None = None
    ingestion_job_id: str | None = None
    retrieval_request_id: str | None = None
    document_id: DocumentId | None = None
    chunk_id: ChunkId | None = None
    actor_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class RepositoryHealth(StrictModel):
    """Provides a sanitized health snapshot for one configured backend."""

    family: StorageFamily
    backend: str
    instance_name: str
    status: HealthStatus
    checked_at: datetime = Field(default_factory=utc_now)
    latency_ms: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
