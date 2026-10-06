"""Translate the neutral application gateway to Temporal history contracts."""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING

from harborrag_runtime.ingestion_contracts import (
    IngestionExecutionReference,
    IngestionExecutionResult,
    IngestionExecutionStatus,
    IngestionRetryRequest,
    PreparedSourceSubmission,
)

from .schemas import RetryFailuresInput, SourceIngestionInput

if TYPE_CHECKING:
    from .client import IngestionTemporalClient
    from .identity import RuntimeWorkflowRef


def to_temporal_source(source: PreparedSourceSubmission) -> SourceIngestionInput:
    """Copy only neutral fields; replay state belongs to the Temporal adapter."""

    return SourceIngestionInput(
        **{field.name: getattr(source, field.name) for field in fields(PreparedSourceSubmission)}
    )


def _reference(reference: RuntimeWorkflowRef) -> IngestionExecutionReference:
    return IngestionExecutionReference(
        run_id=reference.run_id,
        workflow_id=reference.workflow_id,
        first_execution_run_id=reference.first_execution_run_id,
    )


class TemporalIngestionGateway:
    """Keep provider-specific DTOs and operations behind the application port."""

    def __init__(self, client: IngestionTemporalClient) -> None:
        self._client = client

    async def start_ingestion(
        self, source: PreparedSourceSubmission
    ) -> IngestionExecutionReference:
        return _reference(await self._client.start_ingestion(to_temporal_source(source)))

    async def start_retry_failures(
        self, request: IngestionRetryRequest
    ) -> IngestionExecutionReference:
        return _reference(
            await self._client.start_retry_failures(
                RetryFailuresInput(
                    retry_task_id=request.retry_task_id,
                    original_task_id=request.original_task_id,
                    tenant_id=request.tenant_id,
                    document_ids=request.document_ids,
                    document_concurrency=request.document_concurrency,
                )
            )
        )

    async def get_status(self, task_id: str) -> IngestionExecutionStatus:
        status = await self._client.get_status(task_id)
        return IngestionExecutionStatus(
            task_id=status.task_id,
            status=status.status,
            paused=status.paused,
            cancel_requested=status.cancel_requested,
            pause_applied=status.pause_applied,
        )

    async def result(self, task_id: str) -> IngestionExecutionResult:
        result = await self._client.result(task_id)
        return IngestionExecutionResult(
            task_id=result.task_id,
            scan_id=result.scan_id,
            discovered=result.discovered,
            published=result.published,
            unchanged=result.unchanged,
            failed=result.failed,
            removal_candidates=result.removal_candidates,
            unresolved_relations=result.unresolved_relations,
            status=result.status,
        )

    async def get_progress(self, task_id: str) -> dict[str, int]:
        return await self._client.get_progress(task_id)

    async def execution_status(self, task_id: str) -> str:
        return await self._client.execution_status(task_id)

    async def pause(self, task_id: str) -> None:
        await self._client.pause(task_id)

    async def resume(self, task_id: str) -> None:
        await self._client.resume(task_id)

    async def cancel(self, task_id: str) -> None:
        await self._client.cancel(task_id)

    async def health(self) -> bool:
        return await self._client.health()
