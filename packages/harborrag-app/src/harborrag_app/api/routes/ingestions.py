"""Authenticated HTTP boundary for durable Temporal ingestion workflows."""

from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.routes._ingestion_rendering import render_ingestion_response
from harborrag_app.api.schemas import IngestionControlInput, IngestionWorkflowInput
from harborrag_app.workflow_control import BaseAppService

logger = logging.getLogger("harborrag.app.api.ingestions")

router = APIRouter(prefix="/ingestions", tags=["ingestions"])


@router.post("", status_code=202)
async def start_ingestion(
    payload: IngestionWorkflowInput,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> JSONResponse:
    """Submit ingestion through this process's composed application service.

    The CLI builds production composition directly, while this process goes
    through ``select_app_service``, which stubs the control-plane database when
    ``HARBORRAG_ENV=dev`` and no ``HARBORRAG_CONTROL_DB_URL`` is set. Both drive
    the same Temporal ingestion path, but the composed dependencies can differ;
    the selected mode is logged at startup and reported by /diagnostics.
    """

    del principal
    logger.info(
        "Submitting ingestion workflow",
        extra={
            "tenant_id": payload.tenant_id,
            "connector_name": payload.connector_name,
            "requested_run_id": payload.run_id,
        },
    )
    service = _service(request)
    response = await service.start_ingestion(
        tenant_id=payload.tenant_id,
        connector_name=payload.connector_name,
        run_id=payload.run_id,
        manifest_id=payload.manifest_id,
        generation_id=payload.generation_id,
        max_artifacts=payload.max_artifacts,
        wait=payload.wait,
    )
    status_code = 200 if payload.wait else 202
    return render_ingestion_response(
        request,
        response,
        operation="start",
        success_status=status_code,
    )


@router.get("/{run_id}")
async def ingestion_status(
    run_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> JSONResponse:
    """Return live progress and attention queues for an ingestion run."""

    del principal
    response = await _service(request).ingestion_status(run_id)
    return render_ingestion_response(request, response, operation="status")


@router.get("/{run_id}/result")
async def ingestion_result(
    run_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> JSONResponse:
    """Wait for and return the terminal summary for an ingestion run."""

    del principal
    response = await _service(request).ingestion_result(run_id)
    return render_ingestion_response(request, response, operation="result")


@router.post("/{run_id}/actions")
async def control_ingestion(
    run_id: str,
    payload: IngestionControlInput,
    request: Request,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> JSONResponse:
    """Pause, resume, cancel, or retry artifacts in an ingestion run."""

    del principal
    logger.info(
        "Controlling ingestion workflow",
        extra={"run_id": run_id, "action": payload.action},
    )
    response = await _service(request).control_ingestion(
        run_id,
        payload.action,
        artifact_ids=tuple(payload.artifact_ids),
        graceful=payload.graceful,
    )
    return render_ingestion_response(request, response, operation=payload.action)


def _service(request: Request) -> BaseAppService:
    return cast(BaseAppService, request.app.state.app_service)
