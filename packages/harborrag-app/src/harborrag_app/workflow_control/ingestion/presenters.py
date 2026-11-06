from __future__ import annotations

import base64
import json
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from harborrag_core.ingestion import (
    IngestionTask,
    IngestionTaskState,
)
from harborrag_core.invariants import HarborInvariantError

from ..errors import IngestionCursorError
from .models import DocumentPageCursor, TaskPageCursor

TERMINAL_STATES = frozenset(
    {
        IngestionTaskState.COMPLETED,
        IngestionTaskState.PARTIAL,
        IngestionTaskState.FAILED,
        IngestionTaskState.CANCELLED,
    }
)
STATUS_NAMES = {
    IngestionTaskState.PENDING: "PENDING",
    IngestionTaskState.RUNNING: "RUNNING",
    IngestionTaskState.PAUSED: "PAUSED",
    IngestionTaskState.COMPLETED: "SUCCESS",
    IngestionTaskState.PARTIAL: "PARTIAL",
    IngestionTaskState.FAILED: "FAILED",
    IngestionTaskState.CANCELLED: "CANCELLED",
}


def task_response(task: IngestionTask, counts: dict[str, int]) -> dict[str, object]:
    tenant_id = task.request.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HarborInvariantError("stored ingestion task is missing tenant identity")
    summary = task.summary
    succeeded = counts.get("published", 0) + counts.get("success", 0)
    skipped = counts.get("unchanged", 0) + counts.get("skipped", 0)
    failed = counts.get("failed", 0)
    processed = sum(counts.values())
    status = STATUS_NAMES[task.status]
    stage = _task_stage(task)
    return {
        "task_id": task.task_id,
        "tenant": tenant_id,
        "status": status,
        "stage": stage,
        "source": {
            "type": str(task.request["connector_type"]),
            "connection_id": str(task.request["connection_id"]),
        },
        "progress": {
            "discovered": _summary_count(summary, "discovered"),
            "admitted": _summary_count(
                summary,
                "admitted",
                fallback=_summary_count(summary, "discovered"),
            ),
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "removed": _summary_count(summary, "removal_candidates"),
        },
        "submitted_at": task.submitted_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "message": _task_message(status, stage),
    }


def _summary_count(
    summary: dict[str, object],
    key: str,
    *,
    fallback: int = 0,
) -> int:
    value = summary.get(key, fallback)
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _task_stage(task: IngestionTask) -> str:
    if task.status == IngestionTaskState.PENDING:
        return "QUEUED"
    if task.status in TERMINAL_STATES:
        return "COMPLETED"
    stored = task.summary.get("stage")
    if stored in {"PROCESSING_DOCUMENTS", "RECONCILING"}:
        return str(stored)
    return "DISCOVERING"


def _task_message(status: str, stage: str) -> str:
    if stage == "QUEUED":
        return "Ingestion task is queued"
    if stage == "DISCOVERING":
        return "Discovering source documents"
    if stage == "PROCESSING_DOCUMENTS":
        return "Processing admitted documents"
    if stage == "RECONCILING":
        return "Reconciling source removals"
    return {
        "SUCCESS": "Ingestion completed successfully",
        "PARTIAL": "Ingestion completed with document failures",
        "FAILED": "Ingestion failed",
        "CANCELLED": "Ingestion was cancelled",
    }.get(status, "Ingestion completed")


def request_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def task_id() -> str:
    """Generate an opaque public task identifier in canonical UUID form."""
    return str(uuid4())


def encode_cursor(*, task_id: str, position: DocumentPageCursor) -> str:
    return _pack(
        {
            "task_id": task_id,
            "updated_at": position.updated_at,
            "document_id": position.document_id,
        }
    )


def decode_cursor(value: str, *, task_id: str) -> DocumentPageCursor:
    try:
        payload = _unpack(value)
        if payload.get("task_id") != task_id:
            raise ValueError
        updated_at = _cursor_timestamp(payload, "updated_at")
        document_id = str(payload["document_id"])
        if not document_id:
            raise ValueError
        return DocumentPageCursor(updated_at=updated_at, document_id=document_id)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise IngestionCursorError("Document cursor is invalid.", {"field": "cursor"}) from error


def encode_task_cursor(position: TaskPageCursor) -> str:
    return _pack({"submitted_at": position.submitted_at, "task_id": position.task_id})


def decode_task_cursor(value: str) -> TaskPageCursor:
    """Read a task-list keyset position, rejecting anything malformed.

    The cursor carries only a position; the caller binds it to its filters.
    """

    try:
        payload = _unpack(value)
        submitted_at = _cursor_timestamp(payload, "submitted_at")
        task_id = str(payload["task_id"])
        if not task_id:
            raise ValueError
        return TaskPageCursor(submitted_at=submitted_at, task_id=task_id)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise IngestionCursorError("Task cursor is invalid.", {"field": "cursor"}) from error


def _pack(payload: dict[str, str]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _unpack(value: str) -> dict[str, object]:
    padding = "=" * (-len(value) % 4)
    payload = json.loads(base64.urlsafe_b64decode(value + padding))
    if not isinstance(payload, dict):
        raise ValueError
    return payload


def _cursor_timestamp(payload: dict[str, object], key: str) -> str:
    timestamp = str(payload[key])
    datetime.fromisoformat(timestamp)
    return timestamp
