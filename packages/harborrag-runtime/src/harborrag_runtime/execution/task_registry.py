from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_core.contracts.errors import HarborConflictError
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    FailurePage,
    IngestionTask,
    IngestionTaskState,
    StoredFailure,
    TaskDocumentPage,
    TaskDocumentResult,
    TaskPage,
    TaskRegistration,
)
from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.composition.resources import build_ingestion_control
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.source.tasks import pending_source_task, source_scan_id
from harborrag_runtime.ingestion_contracts import PreparedSourceSubmission

from .source_conversion import to_source_request

_PINNED_SETTINGS_KEYS = ("settings_revision", "run_settings")


class IngestionTaskRegistry:
    """Persist the PENDING task before its execution provider is called."""

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
        source: PreparedSourceSubmission,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        actor: str | None = None,
        actor_type: str | None = None,
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
        task = task.model_copy(
            update={"request": _with_actor(task.request, actor=actor, actor_type=actor_type)}
        )
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
        actor: str | None = None,
        actor_type: str | None = None,
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
            **{key: request[key] for key in _PINNED_SETTINGS_KEYS if key in request},
        }
        return await self._control.tasks.register(
            IngestionTask(
                task_id=retry_task_id,
                source_scope_id=original.source_scope_id,
                status=IngestionTaskState.PENDING,
                request=_with_actor(retry_request, actor=actor, actor_type=actor_type),
            )
        )

    async def latest_for_connection(
        self, *, tenant_id: str, connection_id: str
    ) -> IngestionTask | None:
        """The connection's most recently submitted run, or None."""
        return await self._control.connection_records.latest_task(
            tenant_id=tenant_id, connection_id=connection_id
        )

    async def active_for_connection(
        self, *, tenant_id: str, connection_id: str
    ) -> IngestionTask | None:
        """The connection's pending, running or paused run, or None."""
        return await self._control.connection_records.active_task(
            tenant_id=tenant_id, connection_id=connection_id
        )

    async def connection_document_count(self, *, tenant_id: str, connection_id: str) -> int:
        """How many of the connection's documents are published."""
        return await self._control.connection_records.document_count(
            tenant_id=tenant_id, connection_id=connection_id
        )

    async def get(self, task_id: str) -> IngestionTask | None:
        return await self._control.tasks.get(task_id)

    async def pending_submissions(self, *, limit: int = 100) -> tuple[IngestionTask, ...]:
        return await self._control.tasks.pending_submissions(limit=limit)

    async def claim_submission(self, task_id: str) -> str | None:
        return await self._control.tasks.claim_submission(task_id)

    async def release_submission_claim(self, task_id: str, token: str) -> bool:
        return await self._control.tasks.release_submission_claim(task_id, token)

    async def list_tasks(  # noqa: PLR0913 - the listing's filters plus keyset position
        self,
        *,
        tenant_ids: frozenset[str] | None = None,
        statuses: Sequence[str] | None = None,
        before_submitted_at: datetime | None = None,
        before_task_id: str | None = None,
        scope_connection_ids: frozenset[str] | None = None,
        created_after: datetime | None = None,
        limit: int = 50,
    ) -> TaskPage:
        return await self._control.tasks.list_tasks(
            tenant_ids=tenant_ids,
            statuses=statuses,
            before_submitted_at=before_submitted_at,
            before_task_id=before_task_id,
            scope_connection_ids=scope_connection_ids,
            created_after=created_after,
            limit=limit,
        )

    async def list_active(
        self,
        *,
        after_submitted_at: datetime | None = None,
        after_task_id: str | None = None,
        limit: int = 500,
    ) -> tuple[IngestionTask, ...]:
        return await self._control.tasks.list_active(
            after_submitted_at=after_submitted_at, after_task_id=after_task_id, limit=limit
        )

    async def append_task_event(self, task_id: str, event: HarborEvent) -> HarborEvent:
        seq = await self._control.task_events.append_event(task_id, event)
        return replace(event, seq=seq)

    async def list_task_events(
        self,
        task_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 500,
    ) -> list[HarborEvent]:
        return await self._control.task_events.list_events(
            task_id, after_seq=after_seq, limit=limit
        )

    async def update_summary(
        self,
        task_id: str,
        values: Mapping[str, object],
    ) -> None:
        await self._control.tasks.update_summary(task_id, values)

    async def fail_if_unsubmitted(
        self,
        task_id: str,
        *,
        summary: Mapping[str, object],
    ) -> IngestionTaskState | None:
        return await self._control.tasks.fail_if_unsubmitted(task_id, summary=summary)

    async def close_abandoned(
        self, task_id: str, state: IngestionTaskState, *, reason: str
    ) -> None:
        """End a run whose workflow closed without recording it, such as one terminated in Temporal.

        Closes the run's source scan if discovery was still open, then moves the run
        to ``state``, keeping its summary and adding ``reason`` as its ``error_code``.
        A run that already ended is left as it is.

        Args:
            task_id: The run to end.
            state: ``CANCELLED`` or ``FAILED``.
            reason: Why the run ended, such as ``workflow_terminated``.
        """
        scan_id = source_scan_id(task_id)
        if state is IngestionTaskState.CANCELLED:
            await self._control.source_scans.cancel_if_started(scan_id)
        else:
            await self._control.source_scans.fail_if_started(scan_id, safe_reason=reason)
        task = await self._control.tasks.get(task_id)
        if task is None:
            return
        try:
            await self._control.tasks.transition(
                task_id, state, summary={**task.summary, "error_code": reason}
            )
        except HarborConflictError:
            return

    async def progress(self, task_id: str) -> dict[str, int]:
        return await self._control.tasks.progress(task_id)

    async def progress_for_tasks(self, task_ids: Sequence[str]) -> dict[str, dict[str, int]]:
        return await self._control.tasks.progress_for_tasks(task_ids)

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

    async def failure_page(  # noqa: PLR0913 - mirrors the repository
        self,
        *,
        tenant_id: str,
        source_keys: frozenset[str] | None,
        error_category: str | None,
        internal_stages: frozenset[str] | None,
        resolved: bool,
        q: str | None,
        before: tuple[datetime, str, str] | None,
        limit: int,
        include_total: bool,
        known_stages: frozenset[str] | None = None,
    ) -> FailurePage:
        return await self._control.failures.page(
            tenant_id=tenant_id,
            source_keys=source_keys,
            error_category=error_category,
            internal_stages=internal_stages,
            resolved=resolved,
            q=q,
            before=before,
            limit=limit,
            include_total=include_total,
            known_stages=known_stages,
        )

    async def failures_for_retry(  # noqa: PLR0913 - query filters are keyword-only
        self,
        *,
        tenant_id: str,
        keys: Sequence[tuple[str, str]] | None,
        source_keys: frozenset[str] | None,
        error_category: str | None,
        internal_stages: frozenset[str] | None,
        limit: int = 1000,
        known_stages: frozenset[str] | None = None,
    ) -> tuple[StoredFailure, ...]:
        return await self._control.failures.select(
            tenant_id=tenant_id,
            keys=keys,
            source_keys=source_keys,
            error_category=error_category,
            internal_stages=internal_stages,
            limit=limit,
            known_stages=known_stages,
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


def _with_actor(
    request: dict[str, Any], *, actor: str | None, actor_type: str | None
) -> dict[str, Any]:
    """Return ``request`` with who started the run; unchanged when nobody is named."""
    if actor is None:
        return request
    return {**request, "actor": actor, "actor_type": actor_type or "user"}
