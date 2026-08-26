from __future__ import annotations

from hashlib import sha256

from harborrag_core.ingestion import IngestionTask, IngestionTaskState

from .models import SourceIngestionRequest


def source_scan_id(task_id: str) -> str:
    """Build the stable scan identity used by retried discovery activities."""

    normalized = task_id.strip()
    if not normalized:
        raise ValueError("task_id must be non-empty")
    return f"scan:{sha256(normalized.encode('utf-8')).hexdigest()}"


def pending_source_task(request: SourceIngestionRequest) -> IngestionTask:
    """Build the one canonical, runtime-free task record for a source request."""

    query = request.query
    return IngestionTask(
        task_id=request.task_id,
        source_scope_id=request.source_scope_id,
        status=IngestionTaskState.PENDING,
        request={
            "tenant_id": request.tenant_id,
            "connector_name": request.connector_name,
            "connector_type": request.connector_type.value,
            "connection_id": request.connection_id,
            "source_scope_id": request.source_scope_id,
            "configuration_fingerprint": request.configuration_fingerprint,
            "query": {
                "path": query.path,
                "pattern": query.pattern,
                "recursive": query.recursive,
                "updated_after": (
                    query.updated_after.isoformat() if query.updated_after is not None else None
                ),
                "limit": query.limit,
                "include_attachments": query.include_attachments,
                "filters": query.filters,
            },
            "processing": request.processing.model_dump(mode="json"),
            "force_reprocess": request.force_reprocess,
        },
    )
