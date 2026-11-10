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
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Query, Response
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.dependencies import (
    OperationsDependency,
    ResponseContextDependency,
    ScopeDependency,
)
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.api.sse import bounded_sse_frames, with_heartbeat
from harborrag_app.api.v1.admin.connection_schemas import IngestionOut, IngestionStartRequest
from harborrag_app.api.v1.admin.dependencies import error_responses
from harborrag_app.operations.contract import OPERATIONS, route_kwargs
from harborrag_app.workflow_control.ingestion.progress_bridge import event_prefix
from harborrag_core.contracts.events import HarborEvent

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
    responses=error_responses(409, 422, 501),
    **route_kwargs("start_ingestion"),
    dependencies=[Depends(require_role(OPERATIONS["start_ingestion"].role))],
)
async def start_ingestion(
    operations: OperationsDependency,
    scope: ScopeDependency,
    body: IngestionStartRequest,
    response: Response,
) -> IngestionOut:
    """Start an incremental or forced ingestion of a connection; 409 while one is running."""
    accepted = await operations.start_ingestion(scope, body)
    response.headers["Location"] = accepted.location
    return accepted.body


@router.get(
    "",
    responses=LIST_ERROR_RESPONSES,
    **route_kwargs("list_ingestions"),
    dependencies=[Depends(require_role(OPERATIONS["list_ingestions"].role))],
)
async def list_ingestions(
    operations: OperationsDependency,
    scope: ScopeDependency,
    query: Annotated[IngestionListQuery, Query()],
) -> IngestionPage:
    """Newest-first page of the tenant's ingestion runs."""
    return await operations.list_ingestions(scope, query)


@router.get(
    "/{ingestion_id}",
    responses=ERROR_RESPONSES,
    **route_kwargs("get_ingestion"),
    dependencies=[Depends(require_role(OPERATIONS["get_ingestion"].role))],
)
async def get_ingestion(
    ingestion_id: str, operations: OperationsDependency, scope: ScopeDependency
) -> IngestionOut:
    """One ingestion run of the tenant."""
    return await operations.get_ingestion(scope, ingestion_id)


@router.get(
    "/{ingestion_id}/documents",
    responses=ERROR_RESPONSES,
    **route_kwargs("list_ingestion_documents"),
    dependencies=[Depends(require_role(OPERATIONS["list_ingestion_documents"].role))],
)
async def list_ingestion_documents(
    ingestion_id: str,
    operations: OperationsDependency,
    scope: ScopeDependency,
    query: Annotated[IngestionDocumentQuery, Query()],
) -> IngestionDocumentPage:
    """Page of a run's succeeded, skipped and failed documents."""
    return await operations.list_ingestion_documents(scope, ingestion_id, query)


@router.get(
    "/{ingestion_id}/events",
    responses=ERROR_RESPONSES,
    **route_kwargs("stream_ingestion"),
    dependencies=[Depends(require_role(OPERATIONS["stream_ingestion"].role))],
)
async def stream_ingestion(  # noqa: PLR0913 - the route's dependencies and header
    ingestion_id: str,
    operations: OperationsDependency,
    scope: ScopeDependency,
    context: ResponseContextDependency,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse | Response:
    """Replay a run's stored events, then tail it live (SSE), until ``run.completed``.

    The response opens right after the tenant read, which decides the ``404``: headers and
    ``: ping`` frames flow while the run is quiet, and the stream's own deadline
    (``api_stream_timeout_seconds``) ends it with an in-band ``error`` event,
    ``deadline_exceeded``. Nothing here waits on the first event, so the request-level
    deadline never cuts a quiet stream.

    A run that has ended is read first: its events come from the store alone, so that wait is
    bounded, and a reconnect with nothing left after ``Last-Event-ID`` answers ``204``, which
    tells a browser EventSource to stop reconnecting.

    A reconnecting browser EventSource resends the last frame's ``id:`` as the
    ``Last-Event-ID`` header; that becomes ``after_seq``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + context.settings.api_stream_timeout_seconds
    events = await operations.stream_ingestion(
        scope, ingestion_id, after_seq=_parse_after_seq(last_event_id)
    )
    if events is None:
        return Response(status_code=204)

    async def _frames() -> AsyncGenerator[bytes, None]:
        try:
            async for event in events:
                yield _sse_frame(event, ingestion_id)
        finally:
            if isinstance(events, AsyncGenerator):
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


def _sse_frame(event: HarborEvent, ingestion_id: str) -> bytes:
    name = event.name.removeprefix(event_prefix(ingestion_id)).replace("\n", "").replace("\r", "")
    id_line = f"id: {event.seq}\n" if event.seq is not None else ""
    return f"{id_line}event: {name}\ndata: {json.dumps(event.payload, default=str)}\n\n".encode()


@router.post(
    "/{ingestion_id}/pause",
    responses=ERROR_RESPONSES,
    **route_kwargs("pause_ingestion"),
    dependencies=[Depends(require_role(OPERATIONS["pause_ingestion"].role))],
)
async def pause_ingestion(
    ingestion_id: str, operations: OperationsDependency, scope: ScopeDependency
) -> IngestionOut:
    """Pause a run; 409 when its status does not allow it."""
    return await operations.pause_ingestion(scope, ingestion_id)


@router.post(
    "/{ingestion_id}/resume",
    responses=ERROR_RESPONSES,
    **route_kwargs("resume_ingestion"),
    dependencies=[Depends(require_role(OPERATIONS["resume_ingestion"].role))],
)
async def resume_ingestion(
    ingestion_id: str, operations: OperationsDependency, scope: ScopeDependency
) -> IngestionOut:
    """Resume a run; 409 when its status does not allow it."""
    return await operations.resume_ingestion(scope, ingestion_id)


@router.post(
    "/{ingestion_id}/cancel",
    responses=ERROR_RESPONSES,
    **route_kwargs("cancel_ingestion"),
    dependencies=[Depends(require_role(OPERATIONS["cancel_ingestion"].role))],
)
async def cancel_ingestion(
    ingestion_id: str, operations: OperationsDependency, scope: ScopeDependency
) -> IngestionOut:
    """Cancel a run; 409 when its status does not allow it."""
    return await operations.cancel_ingestion(scope, ingestion_id)


@router.post(
    "/{ingestion_id}/retry-failures",
    responses=ERROR_RESPONSES,
    **route_kwargs("retry_ingestion_failures"),
    dependencies=[Depends(require_role(OPERATIONS["retry_ingestion_failures"].role))],
)
async def retry_ingestion_failures(  # noqa: PLR0913 - the route's dependencies and body
    ingestion_id: str,
    operations: OperationsDependency,
    scope: ScopeDependency,
    response: Response,
    request: Annotated[RetryFailuresRequest | None, Body()] = None,
) -> IngestionOut:
    """Start a retry run of the failed documents; 409 while the run is active or has none."""
    accepted = await operations.retry_ingestion_failures(scope, ingestion_id, request)
    response.headers["Location"] = accepted.location
    return accepted.body
