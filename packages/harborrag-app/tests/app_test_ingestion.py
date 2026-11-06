"""Ingestion behavior shared by application-service test doubles."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from harborrag_app.workflow_control.errors import (
    IngestionAlreadyCompletedError,
    IngestionNotFoundError,
)
from harborrag_app.workflow_control.ingestion.models import IngestionCreateCommand
from harborrag_app.workflow_control.schemas import AppResponse


class FixtureIngestionOperations:
    """Run reads of the tenant ``DEFAULT``, which owns every run the doubles know."""

    async def list(self, *, tenant_id: str, **_filters: object) -> dict[str, Any]:
        return {
            "items": [await self.get(tenant_id=tenant_id, ingestion_id="ing_1")],
            "next_cursor": None,
        }

    async def get(self, *, tenant_id: str, ingestion_id: str) -> dict[str, Any]:
        if tenant_id != "DEFAULT":
            raise IngestionNotFoundError(f"ingestion {ingestion_id!r} not found")
        return {
            "ingestion_id": ingestion_id,
            "tenant_id": tenant_id,
            "project_id": "proj-1",
            "connection_id": "smoke-local",
            "mode": "incremental",
            "status": "running",
            "counters": {"discovered": 2, "processed": 1, "skipped": 0, "failed": 0},
            "stages": [],
            "error_category": None,
            "triggered_by": {"principal_type": "user", "principal_id": "u_huy"},
            "settings_revision": "",
            "workflow_id": f"harborrag-source:{ingestion_id}",
            "run_id": ingestion_id,
            "trace_id": "",
            "links": {"temporal": "", "grafana": "", "langfuse": ""},
            "created_at": datetime(2026, 8, 1, 9, 24, tzinfo=UTC),
            "finished_at": None,
        }


class IngestionServiceFixture:
    """Deterministic public and legacy ingestion service behavior."""

    submissions: list[IngestionCreateCommand]
    idempotency: dict[str, str]
    direct_runs: list[dict[str, object]]
    direct_final_status: str = "completed"
    ingestion_operations: Any = FixtureIngestionOperations()

    async def run_ingestion(  # noqa: PLR0913 - mirrors the service port
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str,
        connection_id: str | None = None,
        source_scope_id: str | None = None,
        path: str | None = None,
        pattern: str | None = None,
        recursive: bool = True,
        updated_after: str | None = None,
        max_artifacts: int | None = None,
        include_attachments: bool = True,
        filters: Mapping[str, object] | None = None,
        force_reprocess: bool = False,
    ) -> AppResponse:
        # Every option `ingest run` forwards, so a test can assert the request it made.
        self.direct_runs.append(
            {
                "tenant_id": tenant_id,
                "connector_name": connector_name,
                "run_id": run_id,
                "connection_id": connection_id,
                "source_scope_id": source_scope_id,
                "path": path,
                "pattern": pattern,
                "recursive": recursive,
                "updated_after": updated_after,
                "max_artifacts": max_artifacts,
                "include_attachments": include_attachments,
                "filters": dict(filters or {}),
                "force_reprocess": force_reprocess,
            }
        )
        return AppResponse(
            True,
            {
                "result": {
                    "task_id": run_id,
                    "status": self.direct_final_status,
                    "discovered": 2,
                    "published": 2,
                    "unchanged": 0,
                    "failed": 0,
                }
            },
        )

    async def submit(
        self,
        command: IngestionCreateCommand,
        *,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        self.submissions.append(command)
        task_id = self.idempotency.get(idempotency_key or "")
        if task_id is None:
            task_id = str(uuid4())
            if idempotency_key is not None:
                self.idempotency[idempotency_key] = task_id
        return {
            "task_id": task_id,
            "status": "PENDING",
            "message": "Ingestion task accepted",
            "submitted_at": datetime(2026, 8, 1, 9, 24, tzinfo=UTC),
        }

    async def get_task(self, task_id: str) -> dict[str, object]:
        return {
            "task_id": task_id,
            "tenant": "DEFAULT",
            "status": "RUNNING",
            "stage": "PROCESSING_DOCUMENTS",
            "source": {"type": "local", "connection_id": "smoke-local"},
            "progress": {
                "discovered": 2,
                "admitted": 2,
                "processed": 1,
                "succeeded": 1,
                "failed": 0,
                "skipped": 0,
                "removed": 0,
            },
            "submitted_at": datetime(2026, 8, 1, 9, 24, tzinfo=UTC),
            "started_at": datetime(2026, 8, 1, 9, 24, 2, tzinfo=UTC),
            "completed_at": None,
            "message": "Processing admitted documents",
        }

    async def list_documents(
        self,
        *,
        task_id: str,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        del task_id, status, cursor, limit
        return {
            "items": [
                {
                    "document_id": "document:1",
                    "source_item_id": "adr/0001.md",
                    "document_kind": "file",
                    "title": "ADR-0001",
                    "status": "SUCCESS",
                    "active_document_version_id": "document-version:1",
                    "failure": None,
                    "updated_at": datetime(2026, 8, 1, 9, 25, tzinfo=UTC),
                }
            ],
            "next_cursor": None,
        }

    async def pause(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        if task_id == "complete":
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        return {
            "task_id": task_id,
            "status": "PENDING",
            "message": "Pause requested",
        }

    async def resume(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        if task_id == "complete":
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        return {
            "task_id": task_id,
            "status": "RUNNING",
            "message": "Resume requested",
        }

    async def cancel(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        if task_id == "complete":
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        return {
            "task_id": task_id,
            "status": "RUNNING",
            "message": "Cancellation requested",
        }

    async def retry_failures(
        self,
        *,
        task_id: str,
        document_ids: list[str],
    ) -> dict[str, object]:
        return {
            "task_id": task_id,
            "retry_task_id": str(uuid4()),
            "accepted_document_count": len(document_ids) or 1,
            "message": "Failed documents accepted for retry",
        }

    async def start_ingestion(  # noqa: PLR0913 - mirrors the legacy CLI service port
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str | None = None,
        connection_id: str | None = None,
        source_scope_id: str | None = None,
        path: str | None = None,
        pattern: str | None = None,
        recursive: bool = True,
        updated_after: str | None = None,
        max_artifacts: int | None = None,
        include_attachments: bool = True,
        filters: Mapping[str, object] | None = None,
        force_reprocess: bool = False,
        batch_size: int | None = None,
        document_concurrency: int | None = None,
        wait: bool = False,
    ) -> AppResponse:
        del (
            force_reprocess,
            include_attachments,
            max_artifacts,
            path,
            pattern,
            recursive,
            updated_after,
            wait,
        )
        return AppResponse(
            True,
            {
                "run": {
                    "run_id": run_id or "mock-run",
                    "tenant_id": tenant_id,
                    "connector_name": connector_name,
                    "connection_id": connection_id or connector_name,
                    "source_scope_id": source_scope_id or "mock-scope",
                    "filters": dict(filters or {}),
                    "batch_size": batch_size,
                    "document_concurrency": document_concurrency,
                },
                "workflow": {"workflow_id": "mock-workflow"},
            },
        )

    async def ingestion_status(self, run_id: str) -> AppResponse:
        return AppResponse(True, {"status": {"run_id": run_id, "status": "completed"}})

    async def ingestion_result(self, run_id: str) -> AppResponse:
        return AppResponse(True, {"result": {"run_id": run_id, "status": "completed"}})

    async def control_ingestion(
        self,
        run_id: str,
        action: str,
    ) -> AppResponse:
        return AppResponse(
            True,
            {"run_id": run_id, "action": action},
        )


__all__ = ["IngestionServiceFixture"]
