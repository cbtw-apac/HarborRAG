"""In-process ingestion run and failure inbox operations.

Handles:
- Starting runs through the connection service and reading them back through the run reads
- The tenant guard on a run's event stream, which the event iterator itself does not carry
- Run actions and the failure inbox, with the caller recorded as the actor

Does NOT frame events as SSE or set status codes and headers; the transport does.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from typing import TYPE_CHECKING

from harborrag_app.api.v1.admin.connection_schemas import IngestionOut, IngestionStartRequest
from harborrag_app.api.v1.ingestion.schemas import (
    FailurePage,
    FailureQuery,
    FailureRetryAccepted,
    FailureRetryRequest,
    IngestionDocumentPage,
    IngestionDocumentQuery,
    IngestionListQuery,
    IngestionPage,
    RetryFailuresRequest,
)
from harborrag_app.operations.contract import OPERATIONS
from harborrag_app.operations.errors import ReferenceNotFoundError
from harborrag_app.operations.results import Accepted
from harborrag_app.operations.scope import Scope
from harborrag_app.workflow_control.control_plane.connection_views import ingestion_status
from harborrag_app.workflow_control.control_plane.connections import ConnectionKey
from harborrag_app.workflow_control.ingestion.operations import ControlAction
from harborrag_app.workflow_control.ingestion.presenters import TERMINAL_STATES
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.contracts.events import HarborEvent

if TYPE_CHECKING:
    from harborrag_app.workflow_control.composition.service import AppService

_ENDED_STATUSES = frozenset(ingestion_status(state) for state in TERMINAL_STATES)


class IngestionOps:
    """The ingestion run and failure operations of ``LocalOperations``.

    ``LocalOperations`` sets ``_app_service``, whose run reads, failure inbox and connection
    services run them.
    """

    _app_service: AppService

    async def start_ingestion(
        self, scope: Scope, body: IngestionStartRequest
    ) -> Accepted[IngestionOut]:
        """Start an incremental or forced ingestion of a connection.

        Args:
            scope: Caller and tenant; the caller is recorded as who triggered the run.
            body: The connection, the mode, and the optional item or date narrowing.

        Returns:
            The queued run and the path that follows it.

        Raises:
            ReferenceNotFoundError: The project or the connection does not exist; the
                ``field`` detail names which.
            HarborAlreadyRunningError: The connection already has a run in progress.
            HarborCapabilityError: ``item_refs`` on a source that cannot ingest items.
        """
        scope.require(OPERATIONS["start_ingestion"].role)
        try:
            await self._app_service.project_service.get(
                tenant_id=scope.tenant_id, project_id=body.project_id
            )
        except HarborNotFoundError as exc:
            raise ReferenceNotFoundError(str(exc), {"field": "project_id"}) from exc
        try:
            started = await self._app_service.connection_service.sync(
                ConnectionKey(scope.tenant_id, body.project_id, body.connection_id),
                actor=scope.actor,
                actor_type=scope.actor_type,
                mode=body.mode,
                item_refs=tuple(body.item_refs),
                updated_after=body.updated_after.isoformat() if body.updated_after else None,
            )
        except HarborNotFoundError as exc:
            raise ReferenceNotFoundError(str(exc), {"field": "connection_id"}) from exc
        ingestion = await self._app_service.ingestion_operations.get(
            tenant_id=scope.tenant_id, ingestion_id=started["ingestion_id"]
        )
        return Accepted(
            IngestionOut.model_validate(ingestion),
            f"/v1/admin/ingestions/{ingestion['ingestion_id']}",
        )

    async def list_ingestions(self, scope: Scope, query: IngestionListQuery) -> IngestionPage:
        """List the tenant's ingestion runs, newest first.

        Args:
            scope: Caller and tenant.
            query: Filters, ``cursor`` and page size.

        Raises:
            IngestionCursorError: ``cursor`` is malformed or belongs to other filters.
        """
        scope.require(OPERATIONS["list_ingestions"].role)
        page = await self._app_service.ingestion_operations.list(
            tenant_id=scope.tenant_id,
            project_id=query.project_id,
            connection_ref=query.connection_ref,
            status=query.status,
            created_after=query.created_after,
            cursor=query.cursor,
            limit=query.limit,
        )
        return IngestionPage.model_validate(page)

    async def get_ingestion(self, scope: Scope, ingestion_id: str) -> IngestionOut:
        """Read one ingestion run of the tenant.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
        """
        scope.require(OPERATIONS["get_ingestion"].role)
        return IngestionOut.model_validate(
            await self._app_service.ingestion_operations.get(
                tenant_id=scope.tenant_id, ingestion_id=ingestion_id
            )
        )

    async def list_ingestion_documents(
        self, scope: Scope, ingestion_id: str, query: IngestionDocumentQuery
    ) -> IngestionDocumentPage:
        """List the documents a run succeeded on, skipped or failed.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
            IngestionCursorError: ``query.cursor`` is malformed or belongs to another run.
        """
        scope.require(OPERATIONS["list_ingestion_documents"].role)
        page = await self._app_service.ingestion_operations.documents(
            tenant_id=scope.tenant_id,
            ingestion_id=ingestion_id,
            status=query.status,
            cursor=query.cursor,
            limit=query.limit,
        )
        return IngestionDocumentPage.model_validate(page)

    async def stream_ingestion(
        self, scope: Scope, ingestion_id: str, *, after_seq: int | None
    ) -> AsyncIterator[HarborEvent] | None:
        """Follow a run's stored events, then its live ones, until ``run.completed``.

        The run is read first, which is the only tenant guard the event iterator has. A run
        that has ended is read for its first event before returning, so its wait is bounded.

        Args:
            scope: Caller and tenant.
            ingestion_id: The run to follow.
            after_seq: Resume after this event sequence; ``None`` starts at the first.

        Returns:
            The events, or ``None`` when the run has ended and nothing follows ``after_seq``.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
        """
        scope.require(OPERATIONS["stream_ingestion"].role)
        run = await self._app_service.ingestion_operations.get(
            tenant_id=scope.tenant_id, ingestion_id=ingestion_id
        )
        events = self._app_service.stream_ingestion_events(ingestion_id, after_seq=after_seq)
        if run["status"] not in _ENDED_STATUSES:
            return events
        try:
            first = await events.__anext__()
        except StopAsyncIteration:
            return None
        return _after_first(first, events)

    async def pause_ingestion(self, scope: Scope, ingestion_id: str) -> IngestionOut:
        """Pause a run.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
            IngestionActionStateError: The run's status does not allow it.
        """
        return await self._act(scope, ingestion_id, "pause")

    async def resume_ingestion(self, scope: Scope, ingestion_id: str) -> IngestionOut:
        """Resume a paused run.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
            IngestionActionStateError: The run's status does not allow it.
        """
        return await self._act(scope, ingestion_id, "resume")

    async def cancel_ingestion(self, scope: Scope, ingestion_id: str) -> IngestionOut:
        """Cancel a run.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
            IngestionActionStateError: The run's status does not allow it.
        """
        return await self._act(scope, ingestion_id, "cancel")

    async def retry_ingestion_failures(
        self, scope: Scope, ingestion_id: str, body: RetryFailuresRequest | None
    ) -> Accepted[IngestionOut]:
        """Start a retry run of a finished run's failed documents.

        Args:
            scope: Caller and tenant.
            ingestion_id: The finished run.
            body: Documents to retry; ``None`` or none listed retries every retryable failure.

        Returns:
            The retry run and the path that follows it.

        Raises:
            IngestionNotFoundError: No such run in the tenant.
            IngestionActionStateError: The run is active or has no failed documents.
        """
        scope.require(OPERATIONS["retry_ingestion_failures"].role)
        retry = await self._app_service.ingestion_operations.retry_failures(
            tenant_id=scope.tenant_id,
            ingestion_id=ingestion_id,
            actor=scope.actor,
            actor_type=scope.actor_type,
            document_ids=body.document_ids if body is not None else [],
        )
        return Accepted(
            IngestionOut.model_validate(retry), f"/v1/admin/ingestions/{retry['ingestion_id']}"
        )

    async def list_failures(self, scope: Scope, query: FailureQuery) -> FailurePage:
        """List the tenant's failed documents, newest first, with counts per category."""
        scope.require(OPERATIONS["list_failures"].role)
        page = await self._app_service.failure_inbox.page(
            tenant_id=scope.tenant_id,
            project_id=query.project_id,
            connection_ref=query.connection_ref,
            stage=query.stage,
            error_category=query.error_category,
            resolved=query.resolved,
            q=query.q,
            cursor=query.cursor,
            limit=query.limit,
            include_total=query.include_total,
        )
        return FailurePage.model_validate(page)

    async def retry_failures(self, scope: Scope, body: FailureRetryRequest) -> FailureRetryAccepted:
        """Retry unresolved, retryable failures, one run per root run.

        Raises:
            HarborConflictError: No retryable failure matches the request.
        """
        scope.require(OPERATIONS["retry_failures"].role)
        retried = await self._app_service.failure_inbox.retry(
            tenant_id=scope.tenant_id,
            ids=body.ids,
            filter=body.filter.model_dump(exclude_none=True) if body.filter is not None else None,
            actor=scope.actor,
            actor_type=scope.actor_type,
        )
        return FailureRetryAccepted.model_validate(retried)

    async def _act(self, scope: Scope, ingestion_id: str, action: ControlAction) -> IngestionOut:
        """Run ``action`` on a run, as the operation named after it."""
        scope.require(OPERATIONS[f"{action}_ingestion"].role)
        return IngestionOut.model_validate(
            await self._app_service.ingestion_operations.act(
                tenant_id=scope.tenant_id,
                ingestion_id=ingestion_id,
                action=action,
                actor=scope.actor,
            )
        )


async def _after_first(
    first: HarborEvent, rest: AsyncGenerator[HarborEvent, None]
) -> AsyncGenerator[HarborEvent, None]:
    """Yield ``first``, then ``rest``, closing ``rest`` when the caller stops."""
    try:
        yield first
        async for event in rest:
            yield event
    finally:
        await rest.aclose()
