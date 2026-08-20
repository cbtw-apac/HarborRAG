"""Public ingestion portion of the application-service facade.

Split out of composition/service.py (file-length gate); mixed into AppService
alongside AgentClientMixin/ChatClientMixin/RetrievalClientMixin, following the
same thin-delegate pattern. Every method here forwards to the durable
ingestion application service without adding transport or policy of its own.
"""

from __future__ import annotations

from harborrag_runtime.config.settings import RuntimeSettings

from .connections import connection_catalog
from .models import IngestionCreateCommand
from .service import IngestionApplicationService


class PublicIngestionClientMixin:
    _public_ingestions: IngestionApplicationService
    _settings: RuntimeSettings

    async def submit(
        self,
        command: IngestionCreateCommand,
        *,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        return await self._public_ingestions.submit(
            command,
            idempotency_key=idempotency_key,
        )

    async def get_task(self, task_id: str) -> dict[str, object]:
        return await self._public_ingestions.get_task(task_id)

    async def list_tasks(
        self,
        *,
        tenants: frozenset[str] | None,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        return await self._public_ingestions.list_tasks(
            tenants=tenants,
            status=status,
            cursor=cursor,
            limit=limit,
        )

    async def list_documents(
        self,
        *,
        task_id: str,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        return await self._public_ingestions.list_documents(
            task_id=task_id,
            status=status,
            cursor=cursor,
            limit=limit,
        )

    async def cancel(self, task_id: str) -> dict[str, object]:
        return await self._public_ingestions.cancel(task_id)

    async def retry_failures(
        self,
        *,
        task_id: str,
        document_ids: list[str],
    ) -> dict[str, object]:
        return await self._public_ingestions.retry_failures(
            task_id=task_id,
            document_ids=document_ids,
        )

    async def recover_pending_submissions(self, *, limit: int = 100) -> int:
        return await self._public_ingestions.recover_pending_submissions(limit=limit)

    async def list_connections(self) -> dict[str, object]:
        """Enabled connection identities from the worker's connector catalog."""

        return connection_catalog(self._settings)


__all__ = ["PublicIngestionClientMixin"]
