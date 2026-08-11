"""Ports required by the ingestion application service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    IngestionTask,
    TaskDocumentPage,
    TaskRegistration,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef
from harborrag_runtime.temporal.schemas import RetryFailuresInput, SourceIngestionInput
from harborrag_runtime.temporal.submission import SourceSubmission

type ClientProvider = Callable[[], Awaitable["TemporalGateway"]]
type TaskStoreProvider = Callable[[], Awaitable["PublicTaskStore"]]
type SourceInputBuilder = Callable[[RuntimeSettings, SourceSubmission], SourceIngestionInput]
type TaskIdFactory = Callable[[], str]


class PublicTaskStore(Protocol):
    async def register(
        self,
        source: SourceIngestionInput,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> TaskRegistration: ...

    async def register_retry(
        self,
        *,
        retry_task_id: str,
        original: IngestionTask,
        document_ids: Sequence[str],
    ) -> TaskRegistration: ...

    async def get(self, task_id: str) -> IngestionTask | None: ...

    async def pending_submissions(self, *, limit: int = 100) -> tuple[IngestionTask, ...]: ...

    async def list_active(self, *, limit: int = 500) -> tuple[IngestionTask, ...]: ...

    async def append_task_event(self, task_id: str, event: HarborEvent) -> HarborEvent: ...

    async def list_task_events(
        self,
        task_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 500,
    ) -> list[HarborEvent]: ...

    async def update_summary(
        self,
        task_id: str,
        values: Mapping[str, object],
    ) -> None: ...

    async def progress(self, task_id: str) -> dict[str, int]: ...

    async def document_results_page(
        self,
        task_id: str,
        *,
        statuses: Sequence[str] | None = None,
        after_updated_at: datetime | None = None,
        after_document_id: str | None = None,
        limit: int = 50,
    ) -> TaskDocumentPage: ...

    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> dict[str, ActiveDocumentVersion]: ...


class TemporalGateway(Protocol):
    async def start_ingestion(self, source: SourceIngestionInput) -> RuntimeWorkflowRef: ...

    async def cancel(self, task_id: str) -> None: ...

    async def start_retry_failures(
        self,
        request: RetryFailuresInput,
    ) -> RuntimeWorkflowRef: ...
