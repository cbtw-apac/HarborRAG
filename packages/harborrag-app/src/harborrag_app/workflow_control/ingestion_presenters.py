from __future__ import annotations

import base64
import json
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    IngestionTask,
    IngestionTaskState,
    StoredTaskDocumentResult,
)
from harborrag_core.invariants import HarborInvariantError

from .errors import IngestionCursorError
from .ingestion_models import DocumentPageCursor

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
    IngestionTaskState.COMPLETED: "SUCCESS",
    IngestionTaskState.PARTIAL: "PARTIAL",
    IngestionTaskState.FAILED: "FAILED",
    IngestionTaskState.CANCELLED: "CANCELLED",
}
DOCUMENT_STATUS_NAMES = {
    "pending": "PENDING",
    "processing": "PROCESSING",
    "published": "SUCCESS",
    "success": "SUCCESS",
    "unchanged": "SKIPPED",
    "skipped": "SKIPPED",
    "failed": "FAILED",
    "removed": "REMOVED",
    "cancelled": "CANCELLED",
}
STORAGE_STATUSES = {
    "PENDING": ("pending",),
    "PROCESSING": ("processing",),
    "SUCCESS": ("published", "success"),
    "SKIPPED": ("unchanged", "skipped"),
    "FAILED": ("failed",),
    "REMOVED": ("removed",),
    "CANCELLED": ("cancelled",),
}
FAILURE_STAGES = {
    "FetchAndCaptureRaw": "FETCH",
    "ParseAndNormalize": "PARSE",
    "SyncContentUnits": "CANONICALIZE",
    "PersistCanonical": "CANONICALIZE",
    "ChunkAndValidate": "CHUNK",
    "EncodeChunks": "ENCODE",
    "BuildRelations": "GRAPH_INDEX",
    "BuildProjections": "VECTOR_INDEX",
    "WriteVectorProjection": "VECTOR_INDEX",
    "WriteGraphProjection": "GRAPH_INDEX",
    "VerifyProjections": "VERIFY",
    "PublishVersion": "PUBLISH",
}


def task_response(task: IngestionTask, counts: dict[str, int]) -> dict[str, object]:
    summary = task.summary
    succeeded = counts.get("published", 0) + counts.get("success", 0)
    skipped = counts.get("unchanged", 0) + counts.get("skipped", 0)
    failed = counts.get("failed", 0)
    processed = sum(counts.values())
    status = STATUS_NAMES[task.status]
    stage = _task_stage(task)
    return {
        "task_id": task.task_id,
        "tenant": str(task.request.get("tenant_id") or "DEFAULT"),
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


def document_response(
    result: StoredTaskDocumentResult,
    active: dict[str, ActiveDocumentVersion],
) -> dict[str, object]:
    metadata = result.result
    document_id = str(result.document_id)
    status = DOCUMENT_STATUS_NAMES.get(result.status.lower(), result.status.upper())
    failure = None
    if status == "FAILED":
        internal_stage = str(metadata.get("failure_stage") or "FetchAndCaptureRaw")
        failure = {
            "code": str(metadata.get("safe_error_code") or "DOCUMENT_INGESTION_FAILED"),
            "message": "Document ingestion failed",
            "stage": FAILURE_STAGES.get(internal_stage, "FETCH"),
            "retryable": metadata.get("retryable") is True,
        }
    active_version = active.get(document_id)
    if result.updated_at is None:
        raise HarborInvariantError("result.updated_at must not be None here")
    return {
        "document_id": document_id,
        "source_item_id": str(metadata.get("source_item_id") or document_id),
        "document_kind": str(metadata.get("document_kind") or "document"),
        "title": metadata.get("title"),
        "status": status,
        "active_document_version_id": (
            str(active_version.document_version_id) if active_version is not None else None
        ),
        "failure": failure,
        "updated_at": result.updated_at,
    }


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
    payload = json.dumps(
        {
            "task_id": task_id,
            "updated_at": position.updated_at,
            "document_id": position.document_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str, *, task_id: str) -> DocumentPageCursor:
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if not isinstance(payload, dict) or payload.get("task_id") != task_id:
            raise ValueError
        updated_at = str(payload["updated_at"])
        datetime.fromisoformat(updated_at)
        document_id = str(payload["document_id"])
        if not document_id:
            raise ValueError
        return DocumentPageCursor(updated_at=updated_at, document_id=document_id)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise IngestionCursorError("Document cursor is invalid.") from error
