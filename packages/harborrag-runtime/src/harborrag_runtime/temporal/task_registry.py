from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    IngestionTask,
    IngestionTaskState,
    TaskDocumentPage,
    TaskDocumentResult,
    TaskRegistration,
)
from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.source.tasks import pending_source_task
from harborrag_runtime.ingestion_control_factory import build_ingestion_control

from .conversion import to_source_request
from .schemas import SourceIngestionInput


class IngestionTaskRegistry:
    """Persist the PENDING task before its Temporal workflow is submitted."""

    def __init__(self, control: IngestionControlPlaneDatabase) -> None:
        self._control = control

    @classmethod
    async def connect(
        cls,
        settings: RuntimeSettings,
    ) -> IngestionTaskRegistry:
        control = build_ingestion_control(settings)
        await control.connect()
        if settings.env == "dev":
            await control.provision()
        return cls(control)

    async def register(
        self,
        source: SourceIngestionInput,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> TaskRegistration:
        request = to_source_request(source)
        await self._control.source_scans.register_scope(
            tenant_id=request.tenant_id,
            source_scope_id=request.source_scope_id,
            connector_type=request.connector_type.value,
            connection_id=request.connection_id,
            configuration_fingerprint=request.configuration_fingerprint,
        )
        task = pending_source_task(request)
        return await self._control.tasks.register(
            task,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def register_retry(
        self,
        *,
        retry_task_id: str,
        original: IngestionTask,
        document_ids: Sequence[str],
    ) -> TaskRegistration:
        request = original.request
        tenant_id = request.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise HarborInvariantError("stored ingestion task is missing tenant identity")
        retry_request = {
            "retry_of": original.task_id,
            "tenant_id": tenant_id,
            "document_ids": list(document_ids),
            "connector_name": request["connector_name"],
            "connector_type": request["connector_type"],
            "connection_id": request["connection_id"],
            "source_scope_id": request["source_scope_id"],
        }
        return await self._control.tasks.register(
            IngestionTask(
                task_id=retry_task_id,
                source_scope_id=original.source_scope_id,
                status=IngestionTaskState.PENDING,
                request=retry_request,
            )
        )

    async def get(self, task_id: str) -> IngestionTask | None:
        return await self._control.tasks.get(task_id)

    async def pending_submissions(self, *, limit: int = 100) -> tuple[IngestionTask, ...]:
        return await self._control.tasks.pending_submissions(limit=limit)

    async def update_summary(
        self,
        task_id: str,
        values: Mapping[str, object],
    ) -> None:
        await self._control.tasks.update_summary(task_id, values)

    async def progress(self, task_id: str) -> dict[str, int]:
        return await self._control.tasks.progress(task_id)

    async def document_results_page(
        self,
        task_id: str,
        *,
        statuses: Sequence[str] | None = None,
        after_updated_at: datetime | None = None,
        after_document_id: str | None = None,
        limit: int = 50,
    ) -> TaskDocumentPage:
        return await self._control.tasks.document_results_page(
            task_id,
            statuses=statuses,
            after_updated_at=after_updated_at,
            after_document_id=after_document_id,
            limit=limit,
        )

    async def document_results(self, task_id: str) -> tuple[TaskDocumentResult, ...]:
        return await self._control.tasks.document_results(task_id)

    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> dict[str, ActiveDocumentVersion]:
        return await self._control.document_versions.active_versions(document_ids)

    async def close(self) -> None:
        await self._control.close()
