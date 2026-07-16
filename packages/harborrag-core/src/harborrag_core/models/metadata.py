from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelRequestMetadata(BaseModel):
    """Carry cross-family model request identity and RAG execution context."""

    model_config = ConfigDict(extra="allow", frozen=True)

    request_id: str | None = None
    trace_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    workflow_id: str | None = None
    collection_name: str | None = None
    pipeline_stage: str | None = None
