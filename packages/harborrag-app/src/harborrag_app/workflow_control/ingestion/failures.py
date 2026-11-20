"""Read-side use case behind `GET /v1/admin/failures`."""

from __future__ import annotations

from harborrag_core.ingestion import DocumentFailureRecord

from .ports import TaskStoreProvider


class FailuresService:
    """List a connection's currently-failing documents."""

    def __init__(self, *, task_registry: TaskStoreProvider) -> None:
        self._task_registry = task_registry

    async def list_failures(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        limit: int = 100,
    ) -> tuple[DocumentFailureRecord, ...]:
        store = await self._task_registry()
        return await store.list_failures(
            tenant_id=tenant_id, connection_id=connection_id, limit=limit
        )
