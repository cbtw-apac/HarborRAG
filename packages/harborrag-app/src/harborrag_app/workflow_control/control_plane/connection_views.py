"""Present connections, projects and ingestion runs in the HTTP API's shapes.

Handles:
- the spec's ``Connection`` object, secrets reduced to ``SecretStatus``
- the spec's ``Ingestion`` object from a durable ingestion task
- weak ETags of the form ``W/"rev-<version>"``
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from harborrag_core.domain.connection import Connection
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.security.secret_refs import credentials_view

_ETAG = re.compile(r'^(?:W/)?"rev-(\d+)"$')
_INGESTION_STATUS = {
    IngestionTaskState.PENDING: "queued",
    IngestionTaskState.RUNNING: "running",
    IngestionTaskState.PAUSED: "paused",
    IngestionTaskState.COMPLETED: "succeeded",
    IngestionTaskState.PARTIAL: "succeeded",
    IngestionTaskState.FAILED: "failed",
    IngestionTaskState.CANCELLED: "cancelled",
}


def etag(version: int) -> str:
    """Return the weak ETag of a resource version."""
    return f'W/"rev-{version}"'


def version_from_etag(value: str | None) -> int | None:
    """Parse ``If-Match``; ``None`` when absent, ``-1`` when it names no version of ours."""
    if value is None:
        return None
    match = _ETAG.match(value.strip())
    return int(match.group(1)) if match else -1


def connection_view(
    connection: Connection,
    *,
    last_ingestion: IngestionTask | None = None,
    documents: int | None = None,
) -> dict[str, Any]:
    """Return the spec's ``Connection`` object; secrets never leave as values."""
    return {
        "tenant_id": connection.tenant_id,
        "project_id": connection.project_id,
        "connection_id": connection.connection_id,
        "source_type": connection.source_type,
        "ref": connection.ref,
        "name": connection.name,
        "credentials": credentials_view(connection.credentials),
        "config": dict(connection.config),
        "status": connection.status,
        "last_ingestion": _last_ingestion(last_ingestion),
        "stats": {"documents": documents, "chunks": None, "entities": None, "errors": None}
        | _errors(last_ingestion),
        "created_by": connection.created_by,
        "updated_by": connection.updated_by,
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


def project_view(project: Project, *, connections: int, documents: int) -> dict[str, Any]:
    """Return the spec's ``Project`` object."""
    return {
        "id": project.id,
        "tenant_id": project.tenant_id,
        "name": project.name,
        "description": project.description or None,
        "connection_count": connections,
        "document_count": documents,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def ingestion_view(
    task: IngestionTask,
    *,
    connection_ref: str,
    progress: Mapping[str, int] | None = None,
    triggered_by: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Return the spec's ``Ingestion`` object for a durable task."""
    counts = dict(progress or {})
    summary = task.summary
    request = task.request
    return {
        "id": task.task_id,
        "connection": connection_ref,
        "mode": mode or ("full" if request.get("force_reprocess") else "incremental"),
        "status": _INGESTION_STATUS.get(task.status, "queued"),
        "counters": {
            "discovered": _count(summary, "discovered"),
            "processed": sum(counts.values()),
            "skipped": counts.get("unchanged", 0) + counts.get("skipped", 0),
            "failed": counts.get("failed", 0),
        },
        "stages": [],
        "error_category": summary.get("error_code") if isinstance(summary, Mapping) else None,
        "triggered_by": {"type": "user", "id": triggered_by or str(summary.get("actor", ""))},
        "settings_revision": "",
        "workflow_id": f"harborrag-source:{task.task_id}",
        "run_id": task.task_id,
        "trace_id": "",
        "links": {"temporal": "", "grafana": "", "langfuse": ""},
        "created_at": task.submitted_at,
        "finished_at": task.completed_at,
    }


def _last_ingestion(task: IngestionTask | None) -> dict[str, Any] | None:
    """Reduce the latest run to ``{id, status, finished_at}``."""
    if task is None:
        return None
    finished: datetime | None = task.completed_at
    return {
        "id": task.task_id,
        "status": _INGESTION_STATUS.get(task.status, "queued"),
        "finished_at": finished,
    }


def _errors(task: IngestionTask | None) -> dict[str, int | None]:
    """Failed item count of the latest run, when it recorded one."""
    if task is None:
        return {}
    failed = task.summary.get("failed")
    return {"errors": failed} if isinstance(failed, int) and not isinstance(failed, bool) else {}


def _count(summary: Mapping[str, Any], key: str) -> int:
    """Read an integer counter from a task summary."""
    value = summary.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
