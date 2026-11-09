"""Ingestion runs under ``/v1/admin/ingestions``.

Handles:
- ``POST`` (editor): start an ingestion of one connection
- ``GET`` and ``GET /{ingestion_id}`` (editor): list runs and read one
- ``GET /{ingestion_id}/documents|events`` (editor): a run's documents and its event stream
- ``POST /{ingestion_id}/pause|resume|cancel|retry-failures`` (editor): control a run
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response, status
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.dependencies import ResponseContextDependency
from harborrag_app.api.errors import ReferenceNotFoundError, documented_error_responses
from harborrag_app.api.sse import bounded_sse_frames, with_heartbeat
from harborrag_app.api.v1.admin.connection_schemas import IngestionOut, IngestionStartRequest
from harborrag_app.api.v1.admin.dependencies import Administration, Editor, error_responses
from harborrag_app.workflow_control.control_plane.connections import ConnectionKey
from harborrag_core.contracts.errors import HarborDeadlineExceeded, HarborNotFoundError
from harborrag_core.contracts.events import HarborEvent

from .dependencies import IngestionServiceDependency
from .schemas import (
    IngestionDocumentPage,
    IngestionDocumentQuery,
    IngestionListQuery,
    IngestionPage,
    RetryFailuresRequest,
)

HEARTBEAT_SECONDS = 15.0
_SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}

router = APIRouter(
    prefix="/admin/ingestions",
    tags=["Ingestion"],
    # Function scope, like the chat router: with the default request scope the server
    # deadline stays armed across a StreamingResponse body and hard-cancels the SSE
    # stream mid-flight with no terminal error frame. The capacity lease is still held
    # until the last frame; streams carry their own deadline.
    dependencies=[Depends(require_api_capacity, scope="function")],
)


ERROR_RESPONSES = documented_error_responses(
    {
        404: "Ingestion task not found",
        409: "Action conflicts with task state",
        422: "Invalid ingestion request",
        503: "Ingestion service unavailable",
    }
)
# A listing has no single task to miss and no task state to conflict with, so it
# never answers 404 or 409 -- documenting them would only mislead callers.
LIST_ERROR_RESPONSES = documented_error_responses(
    {
        422: "Invalid listing query or cursor",
        503: "Ingestion service unavailable",
    }
)


@router.post(
    "",
    summary="Start an ingestion",
    response_model=IngestionOut,
    status_code=202,
    responses=error_responses(409, 422, 501),
)
async def start_ingestion(  # noqa: PLR0913 - the route's dependencies and body
    service: Administration,
    operations: IngestionServiceDependency,
    tenant: TenantDependency,
    principal: Editor,
    body: IngestionStartRequest,
    response: Response,
) -> dict[str, Any]:
    """Start an incremental or forced ingestion of a connection; 409 while one is running."""
    try:
        await service.project_service.get(tenant_id=tenant, project_id=body.project_id)
    except HarborNotFoundError as exc:
        raise ReferenceNotFoundError(str(exc), {"field": "project_id"}) from exc
    try:
        started = await service.connection_service.sync(
            ConnectionKey(tenant, body.project_id, body.connection_id),
            actor=principal.subject,
            actor_type=principal.kind,
            mode=body.mode,
            item_refs=tuple(body.item_refs),
            updated_after=body.updated_after.isoformat() if body.updated_after else None,
        )
    except HarborNotFoundError as exc:
        raise ReferenceNotFoundError(str(exc), {"field": "connection_id"}) from exc
    ingestion = await operations.ingestion_operations.get(
        tenant_id=tenant, ingestion_id=started["ingestion_id"]
    )
    response.headers["Location"] = f"/v1/admin/ingestions/{ingestion['ingestion_id']}"
    return ingestion


@router.get(
    "",
    response_model=IngestionPage,
    responses=LIST_ERROR_RESPONSES,
)
async def list_ingestions(
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    _principal: Editor,
    query: Annotated[IngestionListQuery, Query()],
) -> dict[str, Any]:
    """Newest-first page of the tenant's ingestion runs."""
    return await service.ingestion_operations.list(
        tenant_id=tenant,
        project_id=query.project_id,
        connection_ref=query.connection_ref,
        status=query.status,
        created_after=query.created_after,
        cursor=query.cursor,
        limit=query.limit,
    )


@router.get(
    "/{ingestion_id}",
    response_model=IngestionOut,
    responses=ERROR_RESPONSES,
)
async def get_ingestion(
    ingestion_id: str,
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    _principal: Editor,
) -> dict[str, Any]:
    """One ingestion run of the tenant."""
    return await service.ingestion_operations.get(tenant_id=tenant, ingestion_id=ingestion_id)


@router.get(
    "/{ingestion_id}/documents",
    response_model=IngestionDocumentPage,
    responses=ERROR_RESPONSES,
)
async def list_ingestion_documents(
    ingestion_id: str,
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    _principal: Editor,
    query: Annotated[IngestionDocumentQuery, Query()],
) -> dict[str, Any]:
    """Page of a run's succeeded, skipped and failed documents."""
    return await service.ingestion_operations.documents(
        tenant_id=tenant,
        ingestion_id=ingestion_id,
        status=query.status,
        cursor=query.cursor,
        limit=query.limit,
    )


@router.get(
    "/{ingestion_id}/events",
    responses=ERROR_RESPONSES,
)
async def stream_ingestion(
    ingestion_id: str,
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    context: ResponseContextDependency,
    _principal: Editor,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Backlog replay then a live tail of a task's progress events (SSE).

    Pumps the first event outside of StreamingResponse: a missing or other-tenant
    run must raise here, in the plain route coroutine, so normal exception
    handling turns it into the usual enveloped 404 -- once StreamingResponse
    starts streaming, the HTTP status is already committed and an error can no
    longer change it.

    A reconnecting browser EventSource resends the last frame's ``id:`` as
    the ``Last-Event-ID`` header; that becomes ``after_seq`` so the backlog
    replay resumes past what the client already has instead of repeating it.

    The wait for the first event runs under the same ``api_stream_timeout_seconds``
    deadline as the body: before the first byte it answers ``504 deadline_exceeded``,
    after it the stream ends with an ``error`` event.

    Raises:
        HarborDeadlineExceeded: No event arrived before the deadline.
    """
    loop = asyncio.get_running_loop()
    timeout_seconds = context.settings.api_stream_timeout_seconds
    deadline = loop.time() + timeout_seconds
    await service.ingestion_operations.get(tenant_id=tenant, ingestion_id=ingestion_id)
    events = service.stream_ingestion_events(
        ingestion_id, after_seq=_parse_after_seq(last_event_id)
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            first_event = await events.__anext__()
    except TimeoutError as exc:
        await events.aclose()
        raise HarborDeadlineExceeded("Ingestion event stream exceeded its server deadline") from exc
    except StopAsyncIteration:

        async def _empty() -> AsyncIterator[bytes]:
            return
            yield  # pragma: no cover - unreachable, makes this an async generator

        return StreamingResponse(_empty(), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def _frames() -> AsyncGenerator[bytes, None]:
        try:
            yield _sse_frame(first_event)
            async for event in events:
                yield _sse_frame(event)
        finally:
            await events.aclose()

    return StreamingResponse(
        with_heartbeat(
            bounded_sse_frames(
                _frames(),
                timeout_seconds=max(deadline - loop.time(), 0.001),
                error_message="Ingestion event stream exceeded its server deadline",
            ),
            interval_seconds=HEARTBEAT_SECONDS,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _parse_after_seq(last_event_id: str | None) -> int | None:
    if last_event_id is None:
        return None
    try:
        sequence = int(last_event_id)
        return sequence if sequence >= 0 else None
    except ValueError:
        return None


def _sse_frame(event: HarborEvent) -> bytes:
    name = event.name.removeprefix(f"task.{event.trace_id}.").replace("\n", "").replace("\r", "")
    id_line = f"id: {event.seq}\n" if event.seq is not None else ""
    return f"{id_line}event: {name}\ndata: {json.dumps(event.payload, default=str)}\n\n".encode()


@router.post(
    "/{ingestion_id}/pause",
    response_model=IngestionOut,
    responses=ERROR_RESPONSES,
)
async def pause_ingestion(
    ingestion_id: str,
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    principal: Editor,
) -> dict[str, Any]:
    """Pause a run; 409 when its status does not allow it."""
    return await service.ingestion_operations.act(
        tenant_id=tenant, ingestion_id=ingestion_id, action="pause", actor=principal.subject
    )


@router.post(
    "/{ingestion_id}/resume",
    response_model=IngestionOut,
    responses=ERROR_RESPONSES,
)
async def resume_ingestion(
    ingestion_id: str,
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    principal: Editor,
) -> dict[str, Any]:
    """Resume a run; 409 when its status does not allow it."""
    return await service.ingestion_operations.act(
        tenant_id=tenant, ingestion_id=ingestion_id, action="resume", actor=principal.subject
    )


@router.post(
    "/{ingestion_id}/cancel",
    response_model=IngestionOut,
    responses=ERROR_RESPONSES,
)
async def cancel_ingestion(
    ingestion_id: str,
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    principal: Editor,
) -> dict[str, Any]:
    """Cancel a run; 409 when its status does not allow it."""
    return await service.ingestion_operations.act(
        tenant_id=tenant, ingestion_id=ingestion_id, action="cancel", actor=principal.subject
    )


@router.post(
    "/{ingestion_id}/retry-failures",
    response_model=IngestionOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def retry_ingestion_failures(  # noqa: PLR0913 - the route's dependencies and body
    ingestion_id: str,
    service: IngestionServiceDependency,
    tenant: TenantDependency,
    principal: Editor,
    response: Response,
    request: Annotated[RetryFailuresRequest | None, Body()] = None,
) -> dict[str, Any]:
    """Start a retry run of the failed documents; 409 while the run is active or has none."""
    retry = await service.ingestion_operations.retry_failures(
        tenant_id=tenant,
        ingestion_id=ingestion_id,
        actor=principal.subject,
        actor_type=principal.kind,
        document_ids=request.document_ids if request is not None else [],
    )
    response.headers["Location"] = f"/v1/admin/ingestions/{retry['ingestion_id']}"
    return retry
