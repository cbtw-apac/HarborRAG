"""Source types, connections, connection tests, and projects.

Handles:
- ``/v1/admin/source-types``, ``/v1/admin/connections`` and
  ``/v1/admin/projects/{project_id}/connections/{connection_id}[/test]`` (editor)
- ``/v1/admin/projects[/{project_id}]`` (admin)
- ``/v1/projects/{project_id}/connections`` (reader)

Routes parse HTTP into a ``Scope`` and the operation's arguments, call the
operations port, and turn its result into ``ETag`` headers and status codes.

Does NOT start ingestions; ``POST /v1/admin/ingestions`` does.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Response
from fastapi.responses import JSONResponse

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.dependencies import OperationsDependency, ScopeDependency
from harborrag_app.api.pagination import DEFAULT_LIMIT, Page, PageDependency
from harborrag_app.operations.contract import OPERATIONS, route_kwargs
from harborrag_app.operations.scope import ConnectionRef
from harborrag_runtime.config.connectors.models import source_types

from .connection_schemas import (
    ID_PATTERN,
    ConnectionChipPage,
    ConnectionOut,
    ConnectionPage,
    ConnectionStatusName,
    ConnectionTestOut,
    ConnectionTestRequest,
    ConnectionWriteRequest,
    JobAcceptedOut,
    ProjectOut,
    ProjectPage,
    ProjectWriteRequest,
    SourceTypeName,
    SourceTypePage,
)
from .connection_templates import ConnectionTemplateBuilder
from .dependencies import error_responses

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


_errors = error_responses
_TEMPLATES = ConnectionTemplateBuilder(source_types).build()

source_types_router = APIRouter(
    prefix="/admin/source-types",
    tags=["Connections"],
)
connections_router = APIRouter(
    prefix="/admin",
    tags=["Connections"],
)
projects_router = APIRouter(
    prefix="/admin/projects",
    tags=["Projects"],
)
reader_router = APIRouter(
    prefix="/projects",
    tags=["Connections"],
)


@source_types_router.get(
    "",
    summary="List source types",
    **route_kwargs("list_source_types"),
    dependencies=[Depends(require_role(OPERATIONS["list_source_types"].role))],
)
async def list_source_types(
    operations: OperationsDependency,
    scope: ScopeDependency,
    category: Annotated[str | None, Query(pattern=r"^(issue_tracker|wiki|files|code)$")] = None,
) -> SourceTypePage:
    """The data source picker: each type with its credential and config schemas and form hints."""
    return await operations.list_source_types(scope, category=category)


@connections_router.get(
    "/connections",
    summary="List connections",
    responses=_errors(422),
    **route_kwargs("list_connections"),
    dependencies=[Depends(require_role(OPERATIONS["list_connections"].role))],
)
async def list_connections(  # noqa: PLR0913 - the spec's list filters plus paging
    operations: OperationsDependency,
    scope: ScopeDependency,
    project_id: str | None = None,
    source_type: SourceTypeName | None = None,
    status: ConnectionStatusName | None = None,
    page: PageDependency = Page(DEFAULT_LIMIT, None),
) -> ConnectionPage:
    """Every connection in the tenant; ones that need attention first."""
    return await operations.list_connections(
        scope,
        project_id=project_id,
        source_type=source_type,
        status=status,
        limit=page.limit,
        cursor=page.cursor,
    )


@connections_router.get(
    "/projects/{project_id}/connections/{connection_id}",
    summary="Get a connection",
    responses=_errors(404),
    **route_kwargs("get_connection"),
    dependencies=[Depends(require_role(OPERATIONS["get_connection"].role))],
)
async def get_connection(
    operations: OperationsDependency,
    scope: ScopeDependency,
    project_id: ProjectId,
    connection_id: ConnectionId,
    response: Response,
) -> ConnectionOut:
    """One connection; secrets read back as their status, never the value."""
    tagged = await operations.get_connection(scope, ConnectionRef(project_id, connection_id))
    response.headers["ETag"] = tagged.etag
    return tagged.body


@connections_router.put(
    "/projects/{project_id}/connections/{connection_id}",
    summary="Create or replace a connection",
    responses={**_documented(201, ConnectionOut, "Created"), **_errors(404, 409, 412, 422)},
    **route_kwargs("put_connection"),
    dependencies=[Depends(require_role(OPERATIONS["put_connection"].role))],
)
async def put_connection(  # noqa: PLR0913 - path, body, preconditions and caller
    operations: OperationsDependency,
    scope: ScopeDependency,
    project_id: ProjectId,
    connection_id: ConnectionId,
    body: Annotated[ConnectionWriteRequest, Body(openapi_examples=_TEMPLATES)],
    response: Response,
    if_match: IfMatch = None,
    if_none_match: IfNoneMatch = None,
) -> ConnectionOut:
    """Create (201) or overwrite (200) a connection, checked against its source type's schemas."""
    saved = await operations.put_connection(
        scope,
        ConnectionRef(project_id, connection_id),
        body,
        if_match=if_match,
        if_none_match=if_none_match,
    )
    response.status_code = 201 if saved.created else 200
    response.headers["ETag"] = saved.etag
    return saved.body


@connections_router.delete(
    "/projects/{project_id}/connections/{connection_id}",
    summary="Delete a connection",
    responses=_errors(404, 409),
    **route_kwargs("delete_connection"),
    dependencies=[Depends(require_role(OPERATIONS["delete_connection"].role))],
)
async def delete_connection(
    operations: OperationsDependency,
    scope: ScopeDependency,
    project_id: ProjectId,
    connection_id: ConnectionId,
    purge: bool = Query(default=False, description="Also drop Postgres history"),
) -> JobAcceptedOut:
    """Remove the connection's documents, secrets and row in the background."""
    return await operations.delete_connection(
        scope, ConnectionRef(project_id, connection_id), purge=purge
    )


@connections_router.post(
    "/projects/{project_id}/connections/{connection_id}/test",
    summary="Test a connection",
    responses=_errors(404, 422),
    **route_kwargs("test_connection"),
    dependencies=[Depends(require_role(OPERATIONS["test_connection"].role))],
)
async def test_connection(
    operations: OperationsDependency,
    scope: ScopeDependency,
    project_id: ProjectId,
    connection_id: ConnectionId,
    body: Annotated[ConnectionTestRequest | None, Body(openapi_examples=_TEMPLATES)] = None,
) -> ConnectionTestOut:
    """Check form values, or the saved connection without a body; saves nothing."""
    return await operations.test_connection(scope, ConnectionRef(project_id, connection_id), body)


@projects_router.get(
    "",
    summary="List projects",
    responses=_errors(422),
    **route_kwargs("list_projects"),
    dependencies=[Depends(require_role(OPERATIONS["list_projects"].role))],
)
async def list_projects(
    operations: OperationsDependency,
    scope: ScopeDependency,
    q: Annotated[str | None, Query(max_length=255, description="Name search")] = None,
    page: PageDependency = Page(DEFAULT_LIMIT, None),
) -> ProjectPage:
    """Projects in the current tenant."""
    return await operations.list_projects(scope, q=q, limit=page.limit, cursor=page.cursor)


@projects_router.get(
    "/{project_id}",
    summary="Get a project",
    responses=_errors(404),
    **route_kwargs("get_project"),
    dependencies=[Depends(require_role(OPERATIONS["get_project"].role))],
)
async def get_project(
    operations: OperationsDependency,
    scope: ScopeDependency,
    project_id: ProjectId,
    response: Response,
) -> ProjectOut:
    """One project."""
    tagged = await operations.get_project(scope, project_id)
    response.headers["ETag"] = tagged.etag
    return tagged.body


@projects_router.put(
    "/{project_id}",
    summary="Create or replace a project",
    responses={**_documented(201, ProjectOut, "Created"), **_errors(409, 412, 422)},
    **route_kwargs("put_project"),
    dependencies=[Depends(require_role(OPERATIONS["put_project"].role))],
)
async def put_project(  # noqa: PLR0913 - path, body, preconditions and caller
    operations: OperationsDependency,
    scope: ScopeDependency,
    project_id: Annotated[str, Path(pattern=ID_PATTERN, description="Chosen by you")],
    body: ProjectWriteRequest,
    response: Response,
    if_match: IfMatch = None,
    if_none_match: IfNoneMatch = None,
) -> ProjectOut:
    """Create (201) or update (200) a project by the id you choose."""
    saved = await operations.put_project(
        scope, project_id, body, if_match=if_match, if_none_match=if_none_match
    )
    response.status_code = 201 if saved.created else 200
    response.headers["ETag"] = saved.etag
    return saved.body


@projects_router.delete(
    "/{project_id}",
    summary="Delete a project",
    responses={
        **_documented(202, JobAcceptedOut, "cascade=true: removal runs in the background"),
        **_errors(404, 409),
    },
    **route_kwargs("delete_project"),
    dependencies=[Depends(require_role(OPERATIONS["delete_project"].role))],
)
async def delete_project(
    operations: OperationsDependency,
    scope: ScopeDependency,
    project_id: ProjectId,
    cascade: bool = Query(
        default=False, description="Also remove its connections, documents and secrets"
    ),
) -> Response:
    """Delete an empty project at once, or with ``cascade=true`` in the background."""
    accepted = await operations.delete_project(scope, project_id, cascade=cascade)
    if accepted is None:
        return Response(status_code=204)
    return JSONResponse(accepted.model_dump(), status_code=202)


@reader_router.get(
    "/{project_id}/connections",
    summary="List a project's connections",
    responses=_errors(404),
    **route_kwargs("list_searchable_connections"),
    dependencies=[Depends(require_role(OPERATIONS["list_searchable_connections"].role))],
)
async def list_searchable_connections(
    operations: OperationsDependency, scope: ScopeDependency, project_id: ProjectId
) -> ConnectionChipPage:
    """Connections the caller can search in a project, for filter chips."""
    return await operations.list_searchable_connections(scope, project_id)


routers = (source_types_router, connections_router, projects_router, reader_router)
