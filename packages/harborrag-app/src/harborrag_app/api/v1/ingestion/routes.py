"""Thin authenticated routes for the public ingestion task API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Query, status
from fastapi.responses import StreamingResponse

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import documented_error_responses
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.contracts.events import HarborEvent

from .commands import build_ingestion_command
from .dependencies import IngestionServiceDependency
from .schemas import (
    IngestionAcceptedResponse,
    IngestionActionResponse,
    IngestionCreateRequest,
    IngestionDocumentPage,
    IngestionDocumentQuery,
    IngestionTaskResponse,
    RetryAcceptedResponse,
    RetryFailuresRequest,
)

_SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}

router = APIRouter(prefix="/ingestions", tags=["Ingestion"])


def _authorize_task_tenant(principal: Principal, task: dict[str, object]) -> None:
    """Hide task existence when its tenant is outside the caller's scope."""

    if not principal.can_access_tenant(str(task["tenant"])):
        raise HarborNotFoundError("Ingestion task was not found")


ERROR_RESPONSES = documented_error_responses(
    {
        404: "Ingestion task not found",
        409: "Action conflicts with task state",
        422: "Invalid ingestion request",
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
    _authorize_task_tenant(principal, result)
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
    _authorize_task_tenant(principal, task)
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
) -> StreamingResponse:
    """Backlog replay then a live tail of a task's progress events (SSE).

    Pumps the first event outside of StreamingResponse: a missing/
    unauthorized task must raise here, in the plain route coroutine, so
    normal exception handling turns it into the usual enveloped 404 --
    once StreamingResponse starts streaming, the HTTP status is already
    committed and an error can no longer change it.
    """
    task = await service.get_task(task_id)
    _authorize_task_tenant(principal, task)
    events = service.stream_ingestion_events(task_id)
    try:
        first_event = await events.__anext__()
    except StopAsyncIteration:

        async def _empty() -> AsyncIterator[bytes]:
            return
            yield  # pragma: no cover - unreachable, makes this an async generator

        return StreamingResponse(_empty(), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def _frames() -> AsyncIterator[bytes]:
        yield _sse_frame(first_event)
        async for event in events:
            yield _sse_frame(event)

    return StreamingResponse(_frames(), media_type="text/event-stream", headers=_SSE_HEADERS)


def _sse_frame(event: HarborEvent) -> bytes:
    return f"event: {event.name}\ndata: {json.dumps(event.payload, default=str)}\n\n".encode()


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
    _authorize_task_tenant(principal, task)
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
    _authorize_task_tenant(principal, task)
    document_ids = request.document_ids if request is not None else []
    result = await service.retry_failures(
        task_id=task_id,
        document_ids=document_ids,
    )
    return RetryAcceptedResponse.model_validate(result)
