from __future__ import annotations

from typing import Any

from harborrag_core.schemas.state import CheckpointRecord, WorkflowState


def _state_from_row(row: Any) -> WorkflowState:
    return WorkflowState.model_validate(
        {
            "workflow_id": row["workflow_id"],
            "tenant_id": row["tenant_id"],
            "status": row["status"],
            "current_step": row["current_step"],
            "payload": dict(row["payload"] or {}),
            "cursor": dict(row["cursor"] or {}),
            "retry_count": row["retry_count"],
            "version": row["version"],
            "cancellation_requested": row["cancellation_requested"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
        }
    )


def _checkpoint_from_row(row: Any) -> CheckpointRecord:
    return CheckpointRecord.model_validate(
        {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "tenant_id": row["tenant_id"],
            "step_name": row["step_name"],
            "cursor": dict(row["cursor"] or {}),
            "payload": dict(row["payload"] or {}),
            "state_version": row["state_version"],
            "status": row["status"],
            "parent_checkpoint_id": row["parent_checkpoint_id"],
            "created_at": row["created_at"],
        }
    )
