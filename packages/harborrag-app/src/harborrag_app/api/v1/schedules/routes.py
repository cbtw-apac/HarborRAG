"""Authenticated, tenant-scoped schedule management routes."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Path, Query, status

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.workflow_control.scheduling.models import ScheduleCommand

from .dependencies import ScheduleServiceDependency
from .schemas import (
    ScheduleActionRequest,
    ScheduleActionResponse,
    ScheduleBackfillRequest,
    ScheduleListResponse,
    ScheduleResponse,
    ScheduleSourceRequest,
    ScheduleUpsertRequest,
)

router = APIRouter(
    prefix="/schedules",
    tags=["Schedules"],
    dependencies=[Depends(require_api_capacity)],
)

ERROR_RESPONSES = documented_error_responses(
    {
        403: "Schedule role or tenant access denied",
        404: "Schedule not found",
        409: "Schedule conflicts with existing ownership or identity",
        422: "Invalid schedule definition or action",
        503: "Schedule service unavailable",
    }
)

ScheduleIdPath = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]


@router.post(
    "",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
async def create_schedule(
    request: ScheduleUpsertRequest,
    service: ScheduleServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> ScheduleResponse:
    authorize_tenant(principal, request.source.tenant)
    result = await service.create_schedule(_command(request, request.source))
    return ScheduleResponse.model_validate(result)


@router.get("", response_model=ScheduleListResponse, responses=ERROR_RESPONSES)
async def list_schedules(
    service: ScheduleServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    tenant: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> ScheduleListResponse:
    tenant_ids: frozenset[str] | None
    if tenant is not None:
        authorize_tenant(principal, tenant)
        tenant_ids = frozenset({tenant})
    else:
        tenant_ids = principal.tenant_scope
    result = await service.list_schedules(tenant_ids=tenant_ids)
    return ScheduleListResponse.model_validate(result)


@router.get(
    "/{schedule_id}",
    response_model=ScheduleResponse,
    responses=ERROR_RESPONSES,
)
async def get_schedule(
    schedule_id: ScheduleIdPath,
    service: ScheduleServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> ScheduleResponse:
    result = await service.get_schedule(schedule_id)
    authorize_tenant(principal, _tenant(result))
    return ScheduleResponse.model_validate(result)


@router.patch(
    "/{schedule_id}",
    response_model=ScheduleResponse,
    responses=ERROR_RESPONSES,
)
async def update_schedule(
    schedule_id: ScheduleIdPath,
    request: ScheduleUpsertRequest,
    service: ScheduleServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> ScheduleResponse:
    await _authorize_existing(service, schedule_id, principal)
    authorize_tenant(principal, request.source.tenant)
    if request.schedule_id != schedule_id:
        request = request.model_copy(update={"schedule_id": schedule_id})
    result = await service.update_schedule(
        _command(request, request.source, schedule_id=schedule_id)
    )
    return ScheduleResponse.model_validate(result)


@router.post(
    "/{schedule_id}/pause",
    response_model=ScheduleActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def pause_schedule(
    schedule_id: ScheduleIdPath,
    request: ScheduleActionRequest,
    service: ScheduleServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> ScheduleActionResponse:
    await _authorize_existing(service, schedule_id, principal)
    return ScheduleActionResponse.model_validate(
        await service.pause_schedule(schedule_id, note=request.note)
    )


@router.post(
    "/{schedule_id}/unpause",
    response_model=ScheduleActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def unpause_schedule(
    schedule_id: ScheduleIdPath,
    request: ScheduleActionRequest,
    service: ScheduleServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> ScheduleActionResponse:
    await _authorize_existing(service, schedule_id, principal)
    return ScheduleActionResponse.model_validate(
        await service.unpause_schedule(schedule_id, note=request.note)
    )


@router.post(
    "/{schedule_id}/trigger",
    response_model=ScheduleActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def trigger_schedule(
    schedule_id: ScheduleIdPath,
    service: ScheduleServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> ScheduleActionResponse:
    await _authorize_existing(service, schedule_id, principal)
    return ScheduleActionResponse.model_validate(await service.trigger_schedule(schedule_id))


@router.post(
    "/{schedule_id}/backfill",
    response_model=ScheduleActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERROR_RESPONSES,
)
async def backfill_schedule(
    schedule_id: ScheduleIdPath,
    request: ScheduleBackfillRequest,
    service: ScheduleServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> ScheduleActionResponse:
    await _authorize_existing(service, schedule_id, principal)
    return ScheduleActionResponse.model_validate(
        await service.backfill_schedule(
            schedule_id,
            start_at=request.start_at,
            end_at=request.end_at,
        )
    )


@router.delete(
    "/{schedule_id}",
    response_model=ScheduleActionResponse,
    responses=ERROR_RESPONSES,
)
async def delete_schedule(
    schedule_id: ScheduleIdPath,
    service: ScheduleServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> ScheduleActionResponse:
    await _authorize_existing(service, schedule_id, principal)
    return ScheduleActionResponse.model_validate(await service.delete_schedule(schedule_id))


def _command(
    request: ScheduleUpsertRequest,
    source: ScheduleSourceRequest,
    *,
    schedule_id: str | None = None,
) -> ScheduleCommand:
    return ScheduleCommand(
        schedule_id=schedule_id or request.schedule_id,
        workflow=request.workflow,
        cron=request.cron,
        interval_seconds=request.interval_seconds,
        timezone=request.timezone,
        overlap=request.overlap,
        catchup_window_seconds=request.catchup_window_seconds,
        jitter_seconds=request.jitter_seconds,
        pause_on_failure=request.pause_on_failure,
        tenant_id=source.tenant,
        connection_id=source.connection_id,
        source_scope_id=source.source_scope_id,
        force_reprocess=source.mode == "force",
        paused=request.paused,
        note=request.note,
    )


def _tenant(result: dict[str, object]) -> str:
    source = result.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("tenant"), str):
        raise ValueError("schedule response did not include a tenant")
    return cast(str, source["tenant"])


async def _authorize_existing(
    service: ScheduleServiceDependency,
    schedule_id: str,
    principal: Principal,
) -> dict[str, object]:
    result = await service.get_schedule(schedule_id)
    authorize_tenant(principal, _tenant(result))
    return result
