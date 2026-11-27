"""Schedules under ``/v1/admin/schedules``.

Routes parse HTTP into a ``Scope`` and the operation's arguments, call the operations
port, and turn its result into ``ETag`` and ``Location`` headers and status codes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, Response
from fastapi.responses import JSONResponse

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.dependencies import OperationsDependency, ScopeDependency
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.operations.contract import OPERATIONS, dump_body, route_kwargs
from harborrag_app.operations.etags import Preconditions
from harborrag_app.operations.schemas.connections import ID_PATTERN, IngestionOut
from harborrag_app.operations.schemas.schedules import (
    ScheduleListQuery,
    ScheduleOut,
    SchedulePage,
    SchedulePauseRequest,
    ScheduleWriteRequest,
)

from .connection_routes import IfMatch, IfNoneMatch

ScheduleId = Annotated[
    str, Path(pattern=ID_PATTERN, description="Chosen by you; unique in the tenant")
]

_NOT_FOUND = "No such schedule in the tenant (not_found)"
_LIST_ERRORS = documented_error_responses(
    {422: "A filter, the page size or the cursor fails validation (validation_error)"}
)
_READ_ERRORS = documented_error_responses({404: _NOT_FOUND})
_PUT_ERRORS = documented_error_responses(
    {
        404: "A DELETE of the same schedule finished while this PUT ran (not_found)",
        409: "If-None-Match: * and the schedule_id is taken (already_exists), or the "
        "connection is being deleted (invalid_state)",
        412: "If-Match doesn't match the current ETag; re-read and retry (precondition_failed)",
        422: "A field fails the schema or a timetable rule (validation_error), or input names "
        "a project or connection that doesn't exist (reference_not_found)",
    }
)
_PAUSE_ERRORS = documented_error_responses(
    {404: _NOT_FOUND, 422: "The body fails the schema (validation_error)"}
)
_TRIGGER_ERRORS = documented_error_responses(
    {
        404: _NOT_FOUND,
        409: "The connection has a run, named in details.ingestion_id (already_running), or "
        "is being deleted (invalid_state)",
        422: "The schedule's project or connection no longer exists (reference_not_found)",
    }
)

router = APIRouter(
    prefix="/admin/schedules",
    tags=["Schedules"],
    dependencies=[Depends(require_api_capacity, scope="function")],
)


def _preconditions(if_match: IfMatch = None, if_none_match: IfNoneMatch = None) -> Preconditions:
    """Read the ``If-Match`` and ``If-None-Match`` headers of a write."""
    return Preconditions(if_match=if_match, if_none_match=if_none_match)


PreconditionsDependency = Annotated[Preconditions, Depends(_preconditions)]


@router.get(
    "",
    summary="List schedules",
    responses=_LIST_ERRORS,
    **route_kwargs("list_schedules"),
    dependencies=[Depends(require_role(OPERATIONS["list_schedules"].role))],
)
async def list_schedules(
    operations: OperationsDependency,
    scope: ScopeDependency,
    query: Annotated[ScheduleListQuery, Query()],
) -> SchedulePage:
    """Every schedule in the tenant, newest first, each with only its latest slot."""
    return await operations.list_schedules(scope, query)


@router.get(
    "/{schedule_id}",
    summary="Get a schedule",
    responses=_READ_ERRORS,
    **route_kwargs("get_schedule"),
    dependencies=[Depends(require_role(OPERATIONS["get_schedule"].role))],
)
async def get_schedule(
    schedule_id: ScheduleId,
    operations: OperationsDependency,
    scope: ScopeDependency,
    response: Response,
) -> ScheduleOut:
    """One schedule with its last 20 slots, newest first; the ETag header is for If-Match."""
    tagged = await operations.get_schedule(scope, schedule_id)
    response.headers["ETag"] = tagged.etag
    return tagged.body


@router.put(
    "/{schedule_id}",
    summary="Create or replace a schedule",
    responses={201: {"model": ScheduleOut, "description": "Created"}, **_PUT_ERRORS},
    **route_kwargs("put_schedule"),
    dependencies=[Depends(require_role(OPERATIONS["put_schedule"].role))],
)
async def put_schedule(
    schedule_id: ScheduleId,
    operations: OperationsDependency,
    scope: ScopeDependency,
    body: ScheduleWriteRequest,
    preconditions: PreconditionsDependency,
) -> JSONResponse:
    """Create (201) or overwrite (200) a schedule; an overwrite keeps its pause and slots."""
    saved = await operations.put_schedule(scope, schedule_id, body, preconditions=preconditions)
    return JSONResponse(
        dump_body("put_schedule", saved.body),
        status_code=201 if saved.created else 200,
        headers={"ETag": saved.etag},
    )


@router.delete(
    "/{schedule_id}",
    summary="Delete a schedule",
    responses=_READ_ERRORS,
    **route_kwargs("delete_schedule"),
    dependencies=[Depends(require_role(OPERATIONS["delete_schedule"].role))],
)
async def delete_schedule(
    schedule_id: ScheduleId, operations: OperationsDependency, scope: ScopeDependency
) -> None:
    """Remove the schedule and its timer; a run it started keeps going."""
    await operations.delete_schedule(scope, schedule_id)


@router.post(
    "/{schedule_id}/pause",
    summary="Pause a schedule",
    responses=_PAUSE_ERRORS,
    **route_kwargs("pause_schedule"),
    dependencies=[Depends(require_role(OPERATIONS["pause_schedule"].role))],
)
async def pause_schedule(
    schedule_id: ScheduleId,
    operations: OperationsDependency,
    scope: ScopeDependency,
    response: Response,
    body: Annotated[SchedulePauseRequest | None, Body()] = None,
) -> ScheduleOut:
    """Pause the schedule; pausing a paused one returns it unchanged. The ETag is for If-Match."""
    tagged = await operations.pause_schedule(scope, schedule_id, body)
    response.headers["ETag"] = tagged.etag
    return tagged.body


@router.post(
    "/{schedule_id}/resume",
    summary="Resume a schedule",
    responses=_READ_ERRORS,
    **route_kwargs("resume_schedule"),
    dependencies=[Depends(require_role(OPERATIONS["resume_schedule"].role))],
)
async def resume_schedule(
    schedule_id: ScheduleId,
    operations: OperationsDependency,
    scope: ScopeDependency,
    response: Response,
) -> ScheduleOut:
    """Resume the schedule and clear its consecutive failures. The ETag is for If-Match."""
    tagged = await operations.resume_schedule(scope, schedule_id)
    response.headers["ETag"] = tagged.etag
    return tagged.body


@router.post(
    "/{schedule_id}/trigger",
    summary="Trigger a schedule now",
    responses=_TRIGGER_ERRORS,
    **route_kwargs("trigger_schedule"),
    dependencies=[Depends(require_role(OPERATIONS["trigger_schedule"].role))],
)
async def trigger_schedule(
    schedule_id: ScheduleId,
    operations: OperationsDependency,
    scope: ScopeDependency,
    response: Response,
) -> IngestionOut:
    """Start one run now with the schedule's input, even while paused; it acts as the schedule."""
    accepted = await operations.trigger_schedule(scope, schedule_id)
    response.headers["Location"] = accepted.location
    return accepted.body
