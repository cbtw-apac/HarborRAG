"""Public ingestion portion of the application-service facade.

Split out of composition/service.py (file-length gate); mixed into AppService
alongside ChatClientMixin, following the
same thin-delegate pattern. Every method here forwards to the durable
ingestion application service without adding transport or policy of its own.
"""

from __future__ import annotations

from harborrag_engine.ingestion.models import IngestionCreateCommand
from harborrag_runtime.config.settings import RuntimeSettings

from .service import IngestionApplicationService


class PublicIngestionClientMixin:
    """Forward the public ingestion use cases to the ingestion application service."""

    _public_ingestions: IngestionApplicationService
    _settings: RuntimeSettings

    async def submit(
        self,
        command: IngestionCreateCommand,
        *,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        """Register an ingestion and start its workflow."""

        return await self._public_ingestions.submit(
            command,
            idempotency_key=idempotency_key,
        )

    async def get_task(self, task_id: str) -> dict[str, object]:
        """Return the public document of the task ``task_id``."""

        return await self._public_ingestions.get_task(task_id)

    async def pause(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        """Pause a running task; see ``IngestionApplicationService.pause``."""

        return await self._public_ingestions.pause(task_id, actor=actor)

    async def resume(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        """Resume a paused task; see ``IngestionApplicationService.resume``."""

        return await self._public_ingestions.resume(task_id, actor=actor)

    async def cancel(self, task_id: str, *, actor: str | None = None) -> dict[str, object]:
        """Cancel an unfinished task; see ``IngestionApplicationService.cancel``."""

        return await self._public_ingestions.cancel(task_id, actor=actor)

    async def retry_failures(
        self,
        *,
        task_id: str,
        document_ids: list[str],
    ) -> dict[str, object]:
        """Start a retry task for the failed documents of ``task_id``."""

        return await self._public_ingestions.retry_failures(
            task_id=task_id,
            document_ids=document_ids,
        )

    async def recover_pending_submissions(self, *, limit: int = 100) -> int:
        """Restart tasks left between database commit and workflow start."""

        return await self._public_ingestions.recover_pending_submissions(limit=limit)
