from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from harborrag_core.base import StrictModel, utc_now
from harborrag_core.schemas.ids import CheckpointId, TenantId, WorkflowId


class WorkflowStatus(StrEnum):
    """Enumerates supported workflow status values."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"


class WorkflowState(StrictModel):
    """Provides workflow state behavior."""

    workflow_id: WorkflowId
    tenant_id: TenantId
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_step: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    cursor: dict[str, Any] = Field(default_factory=dict)
    retry_count: int = Field(default=0, ge=0)
    version: int = Field(default=1, ge=1)
    cancellation_requested: bool = False
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None


class CheckpointRecord(StrictModel):
    """Represents checkpoint record data shared across HarborRAG layers."""

    id: CheckpointId
    workflow_id: WorkflowId
    tenant_id: TenantId
    step_name: str
    cursor: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    state_version: int = Field(ge=1)
    status: WorkflowStatus = WorkflowStatus.RUNNING
    parent_checkpoint_id: CheckpointId | None = None
    created_at: datetime = Field(default_factory=utc_now)


class LeaseRecord(StrictModel):
    """Represents lease record data shared across HarborRAG layers."""

    resource: str
    owner_token: str
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime
