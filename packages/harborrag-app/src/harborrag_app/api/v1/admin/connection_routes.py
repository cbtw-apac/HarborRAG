"""Source types, connections, connection tests and syncs, and projects.

Handles:
- ``/v1/admin/source-types``, ``/v1/admin/connections`` and
  ``/v1/admin/projects/{project_id}/connections/{connection_id}[/test|/sync]`` (editor)
- ``/v1/admin/projects[/{project_id}]`` (admin)
- ``/v1/projects/{project_id}/connections`` (reader)

Routes translate HTTP to the connection and project services: the tenant from
the credential or ``X-Tenant-Id``, ``ETag`` and ``If-Match``, and status codes.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.workflow_control.control_plane.administration import ConnectionAdministration
from harborrag_app.workflow_control.control_plane.connection_views import (
    etag,
    version_from_etag,
)
from harborrag_app.workflow_control.control_plane.connections import (
    ConnectionKey,
    ConnectionWrite,
)
from harborrag_runtime.config.connectors.models import source_types
from harborrag_runtime.config.connectors.models.spec import Category

from .connection_schemas import (
    ID_PATTERN,
    ConnectionChipPage,
    ConnectionOut,
    ConnectionPage,
    ConnectionPutOut,
    ConnectionStatusName,
    ConnectionTestOut,
    ConnectionTestRequest,
    ConnectionWriteRequest,
    IngestionOut,
    JobAcceptedOut,
    ProjectOut,
    ProjectPage,
    ProjectWriteRequest,
    SourceTypeName,
    SourceTypePage,
    SpecErrorResponse,
    SyncRequest,
)
from .connection_templates import ConnectionTemplateBuilder


def _administration(request: Request) -> ConnectionAdministration:
    """The app service the lifespan composed."""
    return cast(ConnectionAdministration, request.app.state.app_service)


Administration = Annotated[ConnectionAdministration, Depends(_administration)]
Editor = Annotated[Principal, Depends(require_role("editor"))]
Admin = Annotated[Principal, Depends(require_role("admin"))]
ProjectId = Annotated[
    str,
    Path(
        description="Project id, such as default; not its display name",
        openapi_examples={"default": {"summary": "The default project", "value": "default"}},
    ),
]
ConnectionId = Annotated[
    str,
    Path(
        pattern=ID_PATTERN,
        description="Lowercase letters, digits, - and _; a new id creates the connection",
    ),
]
IfMatch = Annotated[
    str | None,
    Header(
        alias="If-Match",
        description="ETag from your last read; the write fails with 412 if someone changed it since",
    ),
]
IfNoneMatch = Annotated[
    str | None,
    Header(alias="If-None-Match", pattern=r"^\*$", description="* makes this create-only"),
]


def _documented(code: int, model: type[Any], description: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entry for an extra success response."""
    return {code: {"model": model, "description": description}}


def _errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entries for the spec's error envelope."""
    return {code: {"model": SpecErrorResponse} for code in codes}


_WRITE_TEMPLATES = ConnectionTemplateBuilder(source_types).with_sync_now(False).build()
_TEST_TEMPLATES = ConnectionTemplateBuilder(source_types).build()

source_types_router = APIRouter(
    prefix="/admin/source-types",
    tags=["Connections"],
    dependencies=[Depends(require_api_capacity), Depends(require_role("editor"))],
)
connections_router = APIRouter(
    prefix="/admin",
    tags=["Connections"],
    dependencies=[Depends(require_api_capacity), Depends(require_role("editor"))],
)
projects_router = APIRouter(
    prefix="/admin/projects",
    tags=["Projects"],
    dependencies=[Depends(require_api_capacity), Depends(require_role("admin"))],
)
reader_router = APIRouter(
    prefix="/projects",
    tags=["Connections"],
    dependencies=[Depends(require_api_capacity), Depends(require_role("reader"))],
)


@source_types_router.get(
    "",
    summary="List source types",
    response_model=SourceTypePage,
    response_model_by_alias=True,
)
async def list_source_types(
    category: Annotated[str | None, Query(pattern=r"^(issue_tracker|wiki|files|code)$")] = None,
) -> dict[str, Any]:
    """The data source picker: each type with its credential and config schemas and form hints."""
    selected = cast(Category | None, category)
    return {"items": [spec.describe() for spec in source_types.all(selected)]}


@connections_router.get(
    "/connections",
    summary="List connections",
    response_model=ConnectionPage,
    responses=_errors(422),
)
async def list_connections(  # noqa: PLR0913 - the spec's list filters plus paging
    service: Administration,
    tenant: TenantDependency,
    project_id: str | None = None,
    source_type: SourceTypeName | None = None,
    status: ConnectionStatusName | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Every connection in the tenant; ones that need attention first."""
    items, next_cursor = await service.connection_service.list_page(
        tenant_id=tenant,
        project_id=project_id,
        source_type=source_type,
        status=status,
        limit=limit,
        cursor=cursor,
    )
    return {"items": items, "next_cursor": next_cursor}


@connections_router.get(
    "/projects/{project_id}/connections/{connection_id}",
    summary="Get a connection",
    response_model=ConnectionOut,
    responses=_errors(404),
)
async def get_connection(
    service: Administration,
    tenant: TenantDependency,
    project_id: ProjectId,
    connection_id: ConnectionId,
    response: Response,
) -> dict[str, Any]:
    """One connection; secrets read back as their status, never the value."""
    view, version = await service.connection_service.get(
        ConnectionKey(tenant, project_id, connection_id)
    )
    response.headers["ETag"] = etag(version)
    return view


@connections_router.put(
    "/projects/{project_id}/connections/{connection_id}",
    summary="Create or replace a connection",
    response_model=ConnectionPutOut,
    response_model_exclude_none=False,
    status_code=200,
    responses={**_documented(201, ConnectionPutOut, "Created"), **_errors(404, 409, 412, 422)},
)
async def put_connection(  # noqa: PLR0913 - path, body, preconditions and caller
    service: Administration,
    tenant: TenantDependency,
    principal: Editor,
    project_id: ProjectId,
    connection_id: ConnectionId,
    body: Annotated[ConnectionWriteRequest, Body(openapi_examples=_WRITE_TEMPLATES)],
    response: Response,
    if_match: IfMatch = None,
    if_none_match: IfNoneMatch = None,
) -> dict[str, Any]:
    """Create (201) or overwrite (200) a connection, checked against its source type's schemas."""
    result = await service.connection_service.put(
        ConnectionKey(tenant, project_id, connection_id),
        ConnectionWrite(
            source_type=body.source_type,
            credentials=_credentials(body.credentials),
            config=body.config,
            name=body.name,
            tuning=body.advanced,
        ),
        actor=principal.subject,
        expected_version=version_from_etag(if_match),
        create_only=if_none_match == "*",
        sync_now=body.sync_now,
    )
    response.status_code = 201 if result.created else 200
    response.headers["ETag"] = etag(result.version)
    return {**result.connection, "ingestion": result.ingestion}


@connections_router.delete(
    "/projects/{project_id}/connections/{connection_id}",
    summary="Delete a connection",
    response_model=JobAcceptedOut,
    status_code=202,
    responses=_errors(404, 409),
)
async def delete_connection(  # noqa: PLR0913 - path, flag and caller
    service: Administration,
    tenant: TenantDependency,
    principal: Editor,
    project_id: ProjectId,
    connection_id: ConnectionId,
    purge: bool = Query(default=False, description="Also drop Postgres history"),
) -> dict[str, str]:
    """Remove the connection's documents, secrets and row in the background."""
    job_id = await service.connection_service.delete(
        ConnectionKey(tenant, project_id, connection_id), actor=principal.subject, purge=purge
    )
    return {"job_id": job_id}


@connections_router.post(
    "/projects/{project_id}/connections/{connection_id}/test",
    summary="Test a connection",
    response_model=ConnectionTestOut,
    responses=_errors(404, 422),
)
async def test_connection(
    service: Administration,
    tenant: TenantDependency,
    project_id: ProjectId,
    connection_id: ConnectionId,
    body: Annotated[ConnectionTestRequest | None, Body(openapi_examples=_TEST_TEMPLATES)] = None,
) -> dict[str, Any]:
    """Check form values, or the saved connection without a body; saves nothing."""
    write = (
        ConnectionWrite(
            source_type=body.source_type,
            credentials=_credentials(body.credentials),
            config=body.config,
            name=body.name,
            tuning=body.advanced,
        )
        if body is not None
        else None
    )
    return await service.connection_service.test(
        ConnectionKey(tenant, project_id, connection_id), write
    )


@connections_router.post(
    "/projects/{project_id}/connections/{connection_id}/sync",
    summary="Start a sync",
    response_model=IngestionOut,
    status_code=202,
    responses=_errors(404, 409, 422, 501),
)
async def sync_connection(  # noqa: PLR0913 - path, body and caller
    service: Administration,
    tenant: TenantDependency,
    principal: Editor,
    project_id: ProjectId,
    connection_id: ConnectionId,
    response: Response,
    body: Annotated[SyncRequest | None, Body()] = None,
) -> dict[str, Any]:
    """Start an incremental or full sync; 409 while one is running."""
    request = body or SyncRequest()
    ingestion = await service.connection_service.sync(
        ConnectionKey(tenant, project_id, connection_id),
        actor=principal.subject,
        mode=request.mode,
        item_refs=tuple(request.item_refs),
        updated_after=request.updated_after.isoformat() if request.updated_after else None,
    )
    response.headers["Location"] = f"/v1/admin/ingestions/{ingestion['id']}"
    return ingestion


@projects_router.get(
    "", summary="List projects", response_model=ProjectPage, responses=_errors(422)
)
async def list_projects(
    service: Administration,
    tenant: TenantDependency,
    q: Annotated[str | None, Query(max_length=255, description="Name search")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Projects in the current tenant."""
    items, next_cursor = await service.project_service.list_page(
        tenant_id=tenant, query=q, limit=limit, cursor=cursor
    )
    return {"items": items, "next_cursor": next_cursor}


@projects_router.get(
    "/{project_id}", summary="Get a project", response_model=ProjectOut, responses=_errors(404)
)
async def get_project(
    service: Administration, tenant: TenantDependency, project_id: ProjectId, response: Response
) -> dict[str, Any]:
    """One project."""
    view, version = await service.project_service.get(tenant_id=tenant, project_id=project_id)
    response.headers["ETag"] = etag(version)
    return view


@projects_router.put(
    "/{project_id}",
    summary="Create or replace a project",
    response_model=ProjectOut,
    status_code=200,
    responses={**_documented(201, ProjectOut, "Created"), **_errors(409, 412, 422)},
)
async def put_project(  # noqa: PLR0913 - path, body, preconditions and caller
    service: Administration,
    tenant: TenantDependency,
    principal: Admin,
    project_id: Annotated[str, Path(pattern=ID_PATTERN, description="Chosen by you")],
    body: ProjectWriteRequest,
    response: Response,
    if_match: IfMatch = None,
    if_none_match: IfNoneMatch = None,
) -> dict[str, Any]:
    """Create (201) or update (200) a project by the id you choose."""
    view, created, version = await service.project_service.put(
        tenant_id=tenant,
        project_id=project_id,
        name=body.name,
        description=body.description,
        actor=principal.subject,
        expected_version=version_from_etag(if_match),
        create_only=if_none_match == "*",
    )
    response.status_code = 201 if created else 200
    response.headers["ETag"] = etag(version)
    return view


@projects_router.delete(
    "/{project_id}",
    summary="Delete a project",
    status_code=204,
    responses={
        **_documented(202, JobAcceptedOut, "cascade=true: removal runs in the background"),
        **_errors(404, 409),
    },
)
async def delete_project(  # noqa: PLR0913 - path, flag and caller
    service: Administration,
    tenant: TenantDependency,
    principal: Admin,
    project_id: ProjectId,
    cascade: bool = Query(
        default=False, description="Also remove its connections, documents and secrets"
    ),
) -> Response:
    """Delete an empty project at once, or with ``cascade=true`` in the background."""
    job_id = await service.project_service.delete(
        tenant_id=tenant, project_id=project_id, actor=principal.subject, cascade=cascade
    )
    if job_id is None:
        return Response(status_code=204)
    return JSONResponse({"job_id": job_id}, status_code=202)


@reader_router.get(
    "/{project_id}/connections",
    summary="List a project's connections",
    response_model=ConnectionChipPage,
    responses=_errors(404),
)
async def list_searchable_connections(
    service: Administration, tenant: TenantDependency, project_id: ProjectId
) -> dict[str, Any]:
    """Connections the caller can search in a project, for filter chips."""
    items = await service.connection_service.searchable(tenant_id=tenant, project_id=project_id)
    return {"items": items}


def _credentials(values: dict[str, Any]) -> dict[str, Any]:
    """Turn request models back into the plain mapping the service takes."""
    return {
        name: value.model_dump() if hasattr(value, "model_dump") else value
        for name, value in values.items()
    }


routers = (source_types_router, connections_router, projects_router, reader_router)
