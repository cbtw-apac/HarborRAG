"""Present connections, projects and ingestion runs in the HTTP API's shapes.

Handles:
- the spec's ``Connection`` object, secrets reduced to ``SecretStatus``
- the spec's ``Ingestion`` object from a durable ingestion task
- weak ETags of the form ``W/"rev-<version>"``
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from harborrag_core.domain.connection import Connection
from harborrag_core.domain.project import Project
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.security.secret_refs import credentials_view

_ETAG = re.compile(r'^(?:W/)?"rev-(\d+)"$')
ANY_VERSION = -2

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
    """Parse ``If-Match``: ``None`` absent, ``ANY_VERSION`` for ``*``, ``-1`` for a foreign tag."""
    if value is None:
        return None
    if value.strip() == "*":
        return ANY_VERSION
    match = _ETAG.match(value.strip())
    return int(match.group(1)) if match else -1


def resolve_any_version(expected: int | None, current: int | None) -> int | None:
    """Turn ``If-Match: *`` into the current version, so it matches any version that exists."""
    if expected == ANY_VERSION and current is not None:
        return current
    return expected


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
        "connection_ref": connection.ref,
        "name": connection.name,
        "credentials": credentials_view(connection.credentials),
        "config": dict(connection.config),
        "advanced": dict(connection.tuning),
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
        "project_id": project.id,
        "tenant_id": project.tenant_id,
        "project_name": project.name,
        "project_description": project.description or None,
        "connection_count": connections,
        "document_count": documents,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


@dataclass(frozen=True)
class IngestionLinks:
    """Operator UI bases a run links to; an unset base yields an empty link."""

    temporal_ui_url: str | None
    temporal_namespace: str
    grafana_url: str | None

    def for_run(self, workflow_id: str, ingestion_id: str) -> dict[str, str]:
        """Return the spec's ``links`` object for one run."""
        temporal = (
            f"{self.temporal_ui_url}/namespaces/{self.temporal_namespace}/workflows/{workflow_id}"
            if self.temporal_ui_url
            else ""
        )
        grafana = (
            f"{self.grafana_url}/d/harborrag-ingestion?var-run={ingestion_id}"
            if self.grafana_url
            else ""
        )
        return {"temporal": temporal, "grafana": grafana, "langfuse": ""}


def ingestion_view(  # noqa: PLR0913 - keyword-only view inputs the callers supply
    task: IngestionTask,
    *,
    connection: Connection | None,
    progress: Mapping[str, int] | None = None,
    triggered_by: str | None = None,
    principal_type: str | None = None,
    mode: str | None = None,
    links: IngestionLinks | None = None,
) -> dict[str, Any]:
    """Return the spec's ``Ingestion`` object for a durable task.

    Args:
        task: The durable run.
        connection: Its connection; ``None`` when deleted, then ids come from the stored source key.
        progress: Result counts by status from the run registry.
        triggered_by: Principal id, when the caller knows it.
        principal_type: Principal type, when the caller knows it.
        mode: Run mode, when the caller knows it.
        links: Operator UI bases; without them every link is empty.
    """
    counts = dict(progress or {})
    summary = task.summary
    request = task.request
    if connection is not None:
        tenant_id, project_id, connection_id = (
            connection.tenant_id,
            connection.project_id,
            connection.connection_id,
        )
    else:
        tenant_id = str(request.get("tenant_id") or "")
        project_id, _, connection_id = str(request.get("connection_id") or "").rpartition("/")
    retry = bool(request.get("retry_of"))
    workflow_id = f"{'harborrag-retry' if retry else 'harborrag-source'}:{task.task_id}"
    discovered = _count(summary, "discovered")
    document_ids = request.get("document_ids")
    if retry and not discovered and isinstance(document_ids, list):
        discovered = len(document_ids)
    return {
        "ingestion_id": task.task_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "connection_id": connection_id,
        "mode": mode or ("force" if request.get("force_reprocess") else "incremental"),
        "status": _INGESTION_STATUS.get(task.status, "queued"),
        "counters": {
            "discovered": discovered,
            "processed": counts.get("published", 0),
            "skipped": counts.get("unchanged", 0) + counts.get("skipped", 0),
            "failed": counts.get("failed", 0),
        },
        "stages": [],
        "error_category": summary.get("error_code") if isinstance(summary, Mapping) else None,
        "triggered_by": {
            "principal_type": principal_type or str(request.get("actor_type") or "user"),
            "principal_id": triggered_by or str(request.get("actor") or summary.get("actor") or ""),
        },
        "settings_revision": str(request.get("configuration_fingerprint") or ""),
        "workflow_id": workflow_id,
        "run_id": task.task_id,
        "trace_id": "",
        "links": (links or IngestionLinks(None, "", None)).for_run(workflow_id, task.task_id),
        "created_at": task.submitted_at,
        "finished_at": task.completed_at,
    }


def _last_ingestion(task: IngestionTask | None) -> dict[str, Any] | None:
    """Reduce the latest run to ``{ingestion_id, status, finished_at}``."""
    if task is None:
        return None
    finished: datetime | None = task.completed_at
    return {
        "ingestion_id": task.task_id,
        "status": _INGESTION_STATUS.get(task.status, "queued"),
        "finished_at": finished,
    }


def _errors(task: IngestionTask | None) -> dict[str, int | None]:
    """Failed item count of the latest run, when it recorded one; 0 before any run."""
    if task is None:
        return {"errors": 0}
    failed = task.summary.get("failed")
    return {"errors": failed} if isinstance(failed, int) and not isinstance(failed, bool) else {}


def _count(summary: Mapping[str, Any], key: str) -> int:
    """Read an integer counter from a task summary."""
    value = summary.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
