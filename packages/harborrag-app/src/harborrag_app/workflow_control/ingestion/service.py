"""Postgres-authoritative application service for the public ingestion API."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborConnectionError,
    HarborValidationError,
)
from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.invariants import require
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.errors import WorkflowOperationError, WorkflowSubmissionError
from harborrag_runtime.execution.source_submission import prepare_source_submission
from harborrag_runtime.ingestion_contracts import (
    IngestionRetryRequest,
    PreparedSourceSubmission,
    SourceSubmission,
)

from ..errors import (
    IngestionAlreadyCompletedError,
    IngestionAlreadyPausedError,
    IngestionAlreadyRunningError,
    IngestionIdempotencyConflictError,
    IngestionNotFoundError,
    IngestionRetryConflictError,
)
from .models import DocumentPageCursor, IngestionCreateCommand
from .ports import (
    ClientProvider,
    PublicTaskStore,
    SourceInputBuilder,
    TaskIdFactory,
    TaskStoreProvider,
)
from .presenters import (
    STATUS_NAMES,
    STORAGE_STATUSES,
    TERMINAL_STATES,
    decode_cursor,
    document_response,
    encode_cursor,
    request_hash,
    task_id,
    task_response,
)
from .recovery import retry_from_task, source_from_task
from .retry_selection import retryable_document_ids
from .task_pages import TaskListingMixin

logger = logging.getLogger("harborrag.app.workflow_control.ingestion")

_SUBMISSION_STATE_KEY = "submission_state"
_SUBMITTED = "submitted"


class IngestionApplicationService(TaskListingMixin):
    """Coordinate API persistence and workflow submission, never ingestion work."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        client_provider: ClientProvider,
        task_store_provider: TaskStoreProvider,
        source_input_builder: SourceInputBuilder = prepare_source_submission,
        task_id_factory: TaskIdFactory | None = None,
    ) -> None:
        self._settings = settings
        self._client_provider = client_provider
        self._task_store_provider = task_store_provider
        self._source_input_builder = source_input_builder
        self._task_id_factory = task_id_factory or task_id

    async def submit(
        self,
        command: IngestionCreateCommand,
        *,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        source = self._build_source(command)
        hashed_request = request_hash(command.public_request)
        store = await self._task_store_provider()
        try:
            registration = await store.register(
                source,
                idempotency_key=idempotency_key,
                request_hash=hashed_request if idempotency_key is not None else None,
            )
        except HarborConflictError as error:
            raise IngestionIdempotencyConflictError(str(error)) from error
        task = registration.task
        if (
            task.status is IngestionTaskState.PENDING
            and task.summary.get(_SUBMISSION_STATE_KEY) != _SUBMITTED
        ):
            submitted_source = source if registration.created else source_from_task(task)
            try:
                workflow = await (await self._client_provider()).start_ingestion(submitted_source)
            except WorkflowSubmissionError as error:
                logger.error(
                    "Ingestion workflow submission failed task_id=%s tenant=%s connection_id=%s",
                    submitted_source.task_id,
                    submitted_source.tenant_id,
                    submitted_source.connection_id,
                )
                raise HarborConnectionError(
                    "Ingestion service is temporarily unavailable."
                ) from error
            await store.update_summary(
                task.task_id,
                {_SUBMISSION_STATE_KEY: _SUBMITTED},
            )
            logger.info(
                "Ingestion submitted task_id=%s workflow_id=%s tenant=%s "
                "connection_id=%s force_reprocess=%s",
                submitted_source.task_id,
                workflow.workflow_id,
                submitted_source.tenant_id,
                submitted_source.connection_id,
                submitted_source.force_reprocess,
            )
        else:
            logger.info(
                "Existing ingestion returned task_id=%s tenant=%s connection_id=%s",
                task.task_id,
                task.request.get("tenant_id"),
                task.request.get("connection_id"),
            )
        submitted_at = require(
            task.submitted_at,
            "registered ingestion task is missing submitted_at",
        )
        return {
            "task_id": task.task_id,
            "status": "PENDING",
            "message": (
                "Ingestion task accepted"
                if registration.created
                else "Existing ingestion task returned"
            ),
            "submitted_at": submitted_at,
        }

    async def get_task(self, task_id: str) -> dict[str, object]:
        store = await self._task_store_provider()
        task = await self._required_task(store, task_id)
        counts = await store.progress(task_id)
        return task_response(task, counts)

    async def recover_pending_submissions(self, *, limit: int = 100) -> int:
        """Restart durable tasks left between database commit and execution start."""

        store = await self._task_store_provider()
        recovered = 0
        for task in await store.pending_submissions(limit=limit):
            try:
                client = await self._client_provider()
                if "retry_of" in task.request:
                    await client.start_retry_failures(retry_from_task(task))
                else:
                    await client.start_ingestion(source_from_task(task))
                await store.update_summary(
                    task.task_id,
                    {_SUBMISSION_STATE_KEY: _SUBMITTED},
                )
            except Exception:  # noqa: BLE001 - one bad task must not starve the outbox
                logger.exception(
                    "Pending ingestion submission recovery failed task_id=%s",
                    task.task_id,
                )
                continue
            recovered += 1
        return recovered

    async def list_documents(
        self,
        *,
        task_id: str,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        store = await self._task_store_provider()
        await self._required_task(store, task_id)
        position = decode_cursor(cursor, task_id=task_id) if cursor else None
        page = await store.document_results_page(
            task_id,
            statuses=STORAGE_STATUSES[status] if status is not None else None,
            after_updated_at=(
                datetime.fromisoformat(position.updated_at) if position is not None else None
            ),
            after_document_id=(position.document_id if position is not None else None),
            limit=limit,
        )
        active = await store.active_versions([str(item.document_id) for item in page.items])
        items = [document_response(item, active) for item in page.items]
        next_cursor = None
        if page.has_more and page.items:
            last = page.items[-1]
            last_updated_at = require(
                last.updated_at,
                "paged document result is missing updated_at",
            )
            next_cursor = encode_cursor(
                task_id=task_id,
                position=DocumentPageCursor(
                    updated_at=last_updated_at.isoformat(),
                    document_id=str(last.document_id),
                ),
            )
        return {"items": items, "next_cursor": next_cursor}

    async def pause(self, task_id: str) -> dict[str, object]:
        store = await self._task_store_provider()
        task = await self._required_task(store, task_id)
        if task.status in TERMINAL_STATES:
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        if task.status is IngestionTaskState.PAUSED:
            raise IngestionAlreadyPausedError("The ingestion task is already paused.")
        try:
            await (await self._client_provider()).pause(task_id)
        except WorkflowOperationError as error:
            raise HarborConnectionError("Ingestion pause is temporarily unavailable.") from error
        return {
            "task_id": task.task_id,
            "status": STATUS_NAMES[IngestionTaskState.PENDING],
            "message": "Pause requested",
        }

    async def resume(self, task_id: str) -> dict[str, object]:
        store = await self._task_store_provider()
        task = await self._required_task(store, task_id)
        if task.status in TERMINAL_STATES:
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        if task.status is IngestionTaskState.RUNNING:
            raise IngestionAlreadyRunningError("The ingestion task is already running.")
        try:
            await (await self._client_provider()).resume(task_id)
        except WorkflowOperationError as error:
            raise HarborConnectionError("Ingestion resume is temporarily unavailable.") from error
        return {
            "task_id": task.task_id,
            "status": STATUS_NAMES[IngestionTaskState.RUNNING],
            "message": "Resume requested",
        }

    async def cancel(self, task_id: str) -> dict[str, object]:
        store = await self._task_store_provider()
        task = await self._required_task(store, task_id)
        if task.status in TERMINAL_STATES:
            raise IngestionAlreadyCompletedError("The ingestion task is already complete.")
        try:
            await (await self._client_provider()).cancel(task_id)
        except WorkflowOperationError as error:
            raise HarborConnectionError(
                "Ingestion cancellation is temporarily unavailable."
            ) from error
        return {
            "task_id": task.task_id,
            "status": STATUS_NAMES[task.status],
            "message": "Cancellation requested",
        }

    async def retry_failures(
        self,
        *,
        task_id: str,
        document_ids: Sequence[str],
    ) -> dict[str, object]:
        store = await self._task_store_provider()
        original = await self._required_task(store, task_id)
        if original.status not in TERMINAL_STATES:
            raise IngestionRetryConflictError(
                "Failed documents can be retried after the ingestion task is complete."
            )
        selected = await retryable_document_ids(
            store,
            task_id=task_id,
            requested=document_ids,
        )
        if not selected:
            raise IngestionRetryConflictError(
                "The ingestion task has no matching retryable document failures."
            )
        retry_task_id = self._task_id_factory()
        registration = await store.register_retry(
            retry_task_id=retry_task_id,
            original=original,
            document_ids=selected,
        )
        try:
            tenant_id = str(original.request.get("tenant_id") or self._settings.ingestion_tenant_id)
            retry = IngestionRetryRequest(
                retry_task_id=retry_task_id,
                original_task_id=task_id,
                tenant_id=tenant_id,
                document_ids=tuple(selected),
            )
            await (await self._client_provider()).start_retry_failures(retry)
        except WorkflowSubmissionError as error:
            raise HarborConnectionError(
                "Ingestion retry service is temporarily unavailable."
            ) from error
        await store.update_summary(
            registration.task.task_id,
            {_SUBMISSION_STATE_KEY: _SUBMITTED},
        )
        return {
            "task_id": task_id,
            "retry_task_id": registration.task.task_id,
            "accepted_document_count": len(selected),
            "message": "Failed documents accepted for retry",
        }

    def _build_source(self, command: IngestionCreateCommand) -> PreparedSourceSubmission:
        try:
            source = self._source_input_builder(
                self._settings,
                SourceSubmission(
                    task_id=self._task_id_factory(),
                    tenant_id=command.tenant_id,
                    connector_name=command.connection_id,
                    connection_id=command.connection_id,
                    source_scope_id=command.source_scope_id,
                    force_reprocess=command.force_reprocess,
                ),
            )
        except ConnectorConfigurationError as error:
            raise HarborValidationError(str(error)) from error
        return source

    @staticmethod
    async def _required_task(
        store: PublicTaskStore,
        task_id: str,
    ) -> IngestionTask:
        task = await store.get(task_id)
        if task is None:
            raise IngestionNotFoundError("Ingestion task was not found.")
        return task
