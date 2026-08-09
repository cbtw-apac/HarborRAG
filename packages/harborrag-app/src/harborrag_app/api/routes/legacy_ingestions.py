"""Deprecated HTTP adapters for Temporal-run ingestion operations."""

from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import error_envelope
from harborrag_app.api.schemas import IngestionControlInput, IngestionWorkflowInput
from harborrag_app.api.v1.ingestion.dependencies import IngestionService
from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_runtime.errors import (
    WorkflowNotFoundError,
    WorkflowNotRetryableError,
    WorkflowNotRunningError,
    WorkflowRunAlreadyStartedError,
)

logger = logging.getLogger("harborrag.app.api.legacy_ingestions")

router = APIRouter(prefix="/ingestions", tags=["ingestions"])

_DEPRECATION_HEADERS = {
    "Deprecation": "true",
    "Sunset": "Sat, 07 Feb 2027 00:00:00 GMT",
    "Link": '</v1/ingestions>; rel="successor-version"',
}


@router.post("", status_code=202, deprecated=True)
async def start_ingestion(
    payload: IngestionWorkflowInput,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> JSONResponse:
    """Submit through the legacy Temporal-run contract."""

    authorize_tenant(principal, payload.tenant_id)
    response = await _service(request).start_ingestion(
        tenant_id=payload.tenant_id,
        connector_name=payload.connector_name,
        run_id=payload.run_id,
        max_artifacts=payload.max_artifacts,
        wait=payload.wait,
    )
    return _render(
        request,
        response,
        operation="start",
        success_status=200 if payload.wait else 202,
    )


@router.get("/{run_id}", deprecated=True)
async def ingestion_status(
    run_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> JSONResponse:
    """Return legacy Temporal-run progress."""

    del principal
    return _render(
        request,
        await _service(request).ingestion_status(run_id),
        operation="status",
    )


@router.get("/{run_id}/result", deprecated=True)
async def ingestion_result(
    run_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> JSONResponse:
    """Return a legacy Temporal-run terminal summary."""

    del principal
    return _render(
        request,
        await _service(request).ingestion_result(run_id),
        operation="result",
    )


@router.post("/{run_id}/actions", deprecated=True)
async def control_ingestion(
    run_id: str,
    payload: IngestionControlInput,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> JSONResponse:
    """Apply a legacy run action through current service operations."""

    del principal, payload.graceful
    service = _service(request)
    if payload.action == "retry":
        result = await cast(IngestionService, service).retry_failures(
            task_id=run_id,
            document_ids=payload.artifact_ids,
        )
        return JSONResponse(
            content=jsonable_encoder(result),
            headers=_DEPRECATION_HEADERS,
        )
    response = await service.control_ingestion(run_id, payload.action)
    return _render(request, response, operation=payload.action)


def _service(request: Request) -> BaseAppService:
    return cast(BaseAppService, request.app.state.app_service)


_CLIENT_ERROR_RESPONSES: dict[str, tuple[int, str]] = {
    WorkflowNotFoundError.__name__: (404, "ingestion_run_not_found"),
    WorkflowNotRunningError.__name__: (409, "ingestion_run_not_running"),
    WorkflowNotRetryableError.__name__: (409, "ingestion_artifacts_not_retryable"),
    WorkflowRunAlreadyStartedError.__name__: (409, "ingestion_run_already_started"),
}


def _render(
    request: Request,
    response: AppResponse,
    *,
    operation: str,
    success_status: int = 200,
) -> JSONResponse:
    if response.ok:
        return JSONResponse(
            status_code=success_status,
            content=jsonable_encoder(response.data),
            headers=_DEPRECATION_HEADERS,
        )

    error_type = str(response.data.get("error_type", "runtime_operation_failed"))
    status_code, code = _CLIENT_ERROR_RESPONSES.get(
        error_type,
        (502, "ingestion_operation_failed"),
    )
    logger.warning(
        "Legacy ingestion operation failed",
        extra={"operation": operation, "error_type": error_type, "status_code": status_code},
    )
    return JSONResponse(
        status_code=status_code,
        content=error_envelope(
            request,
            code,
            response.error or "Ingestion operation failed",
            {"operation": operation, "error_type": error_type},
        ),
        headers=_DEPRECATION_HEADERS,
    )
