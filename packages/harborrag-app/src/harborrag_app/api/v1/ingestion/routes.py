"""Thin authenticated routes for the public ingestion task API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Query, status
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.dependencies import (
    authorize_task_tenant,
    authorize_tenant,
    require_role,
)
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.errors import documented_error_responses
from harborrag_core.contracts.events import HarborEvent

from .commands import build_ingestion_command
from .dependencies import IngestionServiceDependency
from .schemas import (
    IngestionAcceptedResponse,
    IngestionActionResponse,
    IngestionCreateRequest,
    IngestionDocumentPage,
    IngestionDocumentQuery,
    IngestionTaskPage,
    IngestionTaskQuery,
    IngestionTaskResponse,
    RetryAcceptedResponse,
    RetryFailuresRequest,
)

_SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}

router = APIRouter(
    prefix="/ingestions",
    tags=["Ingestion"],
    dependencies=[Depends(require_api_capacity)],
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
    response_model=IngestionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def create_ingestion(
    request: IngestionCreateRequest,
    service: IngestionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ] = None,
) -> IngestionAcceptedResponse:
    authorize_tenant(principal, request.tenant)
    result = await service.submit(
        build_ingestion_command(request),
        idempotency_key=idempotency_key,
    )
    return IngestionAcceptedResponse.model_validate(result)


@router.get(
    "",
    response_model=IngestionTaskPage,
    responses=LIST_ERROR_RESPONSES,
)
async def list_ingestions(
    service: IngestionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    query: Annotated[IngestionTaskQuery, Query()],
) -> IngestionTaskPage:
    """Newest-first page of the caller's ingestion tasks.

    The tenant filter is resolved to the caller's authorized scope before the
    query runs, so an unscoped listing can never spill another tenant's tasks
    -- unlike the by-ID routes, there is no single record to authorize after
    the fact.
    """
    result = await service.list_tasks(
        tenants=_authorized_tenants(principal, query.tenant),
        statuses=[value.value for value in query.status] if query.status else None,
        cursor=query.cursor,
        limit=query.limit,
    )
    return IngestionTaskPage.model_validate(result)


def _authorized_tenants(principal: Principal, tenant: str | None) -> frozenset[str] | None:
    """Resolve the requested tenant filter into the scope this caller may read.

    An explicit tenant outside the caller's scope is a rejected filter rather
    than a hidden resource, so it is a 403 like ``POST /v1/ingestions`` -- it
    reveals nothing about whether that tenant has any tasks. Omitting it falls
    back to the caller's own scope, where None means an unrestricted principal.
    """

    if tenant is None:
        return principal.tenant_scope
    authorize_tenant(principal, tenant)
    return frozenset({tenant})


@router.get(
    "/{task_id}",
    response_model=IngestionTaskResponse,
    responses=ERROR_RESPONSES,
)
async def get_ingestion(
    task_id: str,
    service: IngestionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> IngestionTaskResponse:
    result = await service.get_task(task_id)
    authorize_task_tenant(principal, result)
    return IngestionTaskResponse.model_validate(result)


@router.get(
    "/{task_id}/documents",
    response_model=IngestionDocumentPage,
    responses=ERROR_RESPONSES,
)
async def list_ingestion_documents(
    task_id: str,
    service: IngestionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    query: Annotated[IngestionDocumentQuery, Query()],
) -> IngestionDocumentPage:
    task = await service.get_task(task_id)
    authorize_task_tenant(principal, task)
    result = await service.list_documents(
        task_id=task_id,
        status=query.status.value if query.status is not None else None,
        cursor=query.cursor,
        limit=query.limit,
    )
    return IngestionDocumentPage.model_validate(result)


@router.get(
    "/{task_id}/stream",
    responses=ERROR_RESPONSES,
)
async def stream_ingestion(
    task_id: str,
    service: IngestionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Backlog replay then a live tail of a task's progress events (SSE).

    Pumps the first event outside of StreamingResponse: a missing/
    unauthorized task must raise here, in the plain route coroutine, so
    normal exception handling turns it into the usual enveloped 404 --
    once StreamingResponse starts streaming, the HTTP status is already
    committed and an error can no longer change it.

    A reconnecting browser EventSource resends the last frame's ``id:`` as
    the ``Last-Event-ID`` header; that becomes ``after_seq`` so the backlog
    replay resumes past what the client already has instead of repeating it.
    """
    task = await service.get_task(task_id)
    authorize_task_tenant(principal, task)
    events = service.stream_ingestion_events(task_id, after_seq=_parse_after_seq(last_event_id))
    try:
        first_event = await events.__anext__()
    except StopAsyncIteration:

        async def _empty() -> AsyncIterator[bytes]:
            return
            yield  # pragma: no cover - unreachable, makes this an async generator

        return StreamingResponse(_empty(), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def _frames() -> AsyncIterator[bytes]:
        try:
            yield _sse_frame(first_event)
            async for event in events:
                yield _sse_frame(event)
        finally:
            await events.aclose()

    return StreamingResponse(_frames(), media_type="text/event-stream", headers=_SSE_HEADERS)


def _parse_after_seq(last_event_id: str | None) -> int | None:
    if last_event_id is None:
        return None
    try:
        sequence = int(last_event_id)
        return sequence if sequence >= 0 else None
    except ValueError:
        return None


def _sse_frame(event: HarborEvent) -> bytes:
    name = event.name.replace("\n", "").replace("\r", "")
    id_line = f"id: {event.seq}\n" if event.seq is not None else ""
    return f"{id_line}event: {name}\ndata: {json.dumps(event.payload, default=str)}\n\n".encode()


@router.post(
    "/{task_id}/cancel",
    response_model=IngestionActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def cancel_ingestion(
    task_id: str,
    service: IngestionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> IngestionActionResponse:
    task = await service.get_task(task_id)
    authorize_task_tenant(principal, task)
    return IngestionActionResponse.model_validate(await service.cancel(task_id))


@router.post(
    "/{task_id}/retry-failures",
    response_model=RetryAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def retry_ingestion_failures(
    task_id: str,
    service: IngestionServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
    request: Annotated[RetryFailuresRequest | None, Body()] = None,
) -> RetryAcceptedResponse:
    task = await service.get_task(task_id)
    authorize_task_tenant(principal, task)
    document_ids = request.document_ids if request is not None else []
    result = await service.retry_failures(
        task_id=task_id,
        document_ids=document_ids,
    )
    return RetryAcceptedResponse.model_validate(result)
