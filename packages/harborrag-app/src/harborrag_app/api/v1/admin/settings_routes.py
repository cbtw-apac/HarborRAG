"""Runtime settings per tenant, project and connection.

Handles:
- ``GET /v1/admin/settings`` (admin; editor for ``connection:`` levels)
- ``PATCH /v1/admin/settings`` (admin)
- ``GET /v1/admin/settings/schema`` (editor)

Routes parse the ``level`` query and ``If-Match`` into a ``Scope`` and the operation's
arguments, call the operations port, and set the ``ETag`` it returns.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.dependencies import OperationsDependency, ScopeDependency
from harborrag_app.operations.contract import OPERATIONS, route_kwargs

from .dependencies import error_responses
from .settings_schemas import SettingsOut, SettingsPatchOut

LevelQuery = Annotated[
    str | None,
    Query(description="tenant (default), project:<id> or connection:<project_id>/<id>"),
]
IfMatch = Annotated[
    str | None,
    Header(
        alias="If-Match",
        description="ETag from your last read, or *; the write fails with 412 if settings changed since",
    ),
]

router = APIRouter(prefix="/admin/settings", tags=["Settings"])


@router.get(
    "",
    summary="Read settings at a level",
    responses=error_responses(403, 422),
    **route_kwargs("read_settings"),
    dependencies=[Depends(require_role(OPERATIONS["read_settings"].role))],
)
async def read_settings(
    operations: OperationsDependency,
    scope: ScopeDependency,
    response: Response,
    level: LevelQuery = None,
    effective: bool = False,
) -> SettingsOut:
    """Overrides stored at the level, or every key's effective value with its origin."""
    read = await operations.read_settings(scope, level=level, effective=effective)
    response.headers["ETag"] = read.etag
    return read.body


@router.patch(
    "",
    summary="Change settings at a level",
    responses=error_responses(403, 409, 412, 422),
    **route_kwargs("patch_settings"),
    dependencies=[Depends(require_role(OPERATIONS["patch_settings"].role))],
)
async def patch_settings(  # noqa: PLR0913 - body, level, flags, precondition and caller
    operations: OperationsDependency,
    scope: ScopeDependency,
    body: Annotated[
        dict[str, Any], Body(description="Key to new value; null removes the override")
    ],
    response: Response,
    level: LevelQuery = None,
    dry_run: bool = False,
    reindex: bool = False,
    if_match: IfMatch = None,
) -> SettingsPatchOut:
    """Save the changes as the next revision; nothing is saved when any key is refused."""
    patched = await operations.patch_settings(
        scope, body, level=level, dry_run=dry_run, reindex=reindex, if_match=if_match
    )
    response.headers["ETag"] = patched.etag
    return patched.body


@router.get(
    "/schema",
    summary="JSON schema of every setting",
    responses=error_responses(403),
    **route_kwargs("settings_schema"),
    dependencies=[Depends(require_role(OPERATIONS["settings_schema"].role))],
)
async def settings_schema(
    operations: OperationsDependency, scope: ScopeDependency
) -> dict[str, Any]:
    """The registry's settings by group: type, range, levels, when a change applies, description."""
    return await operations.settings_schema(scope)
