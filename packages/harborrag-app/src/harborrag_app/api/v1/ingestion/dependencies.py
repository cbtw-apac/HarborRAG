"""Application-service dependency for ingestion routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.ingestion.models import IngestionCreateCommand
from harborrag_core.contracts.events import HarborEvent


class IngestionService(Protocol):
    """Narrow application facade required by the ingestion transport."""

    async def submit(
        self,
        command: IngestionCreateCommand,
        *,
        idempotency_key: str | None,
    ) -> dict[str, object]: ...

    async def get_task(self, task_id: str) -> dict[str, object]: ...

    async def list_documents(
        self,
        *,
        task_id: str,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]: ...

    async def cancel(self, task_id: str) -> dict[str, object]: ...

    async def retry_failures(
        self,
        *,
        task_id: str,
        document_ids: list[str],
    ) -> dict[str, object]: ...

    def stream_ingestion_events(self, task_id: str) -> AsyncIterator[HarborEvent]: ...


def ingestion_service(request: Request) -> IngestionService:
    return cast(IngestionService, request.app.state.app_service)


IngestionServiceDependency = Annotated[IngestionService, Depends(ingestion_service)]
