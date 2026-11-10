"""Runtime settings per tenant, project and connection.

Handles:
- ``GET /v1/admin/settings`` (admin; editor for ``connection:`` levels)
- ``PATCH /v1/admin/settings`` (admin)
- ``GET /v1/admin/settings/schema`` (editor)

Routes translate HTTP to the settings service: the tenant from the credential,
the ``level`` query, ``ETag`` and the required ``If-Match``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.principal import Principal, authorize_role
from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.operations.contract import OPERATIONS, route_kwargs
from harborrag_app.workflow_control.control_plane.connection_views import (
    etag,
    version_from_etag,
)
from harborrag_core.config_store import SettingsLevel, revision_label
from harborrag_core.contracts.errors import HarborValidationError

from .dependencies import SettingsDependency, error_responses

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
)
async def read_settings(  # noqa: PLR0913 - tenant, caller, level, view and response header
    service: SettingsDependency,
    tenant: TenantDependency,
    principal: Annotated[Principal, Depends(require_role(OPERATIONS["read_settings"].role))],
    response: Response,
    level: LevelQuery = None,
    effective: bool = False,
) -> dict[str, Any]:
    """Overrides stored at the level, or every key's effective value with its origin."""
    parsed = SettingsLevel.parse(level)
    if parsed.kind != "connection":
        authorize_role(principal, "admin")
    result = await service.read(tenant_id=tenant, level=parsed, effective=effective)
    response.headers["ETag"] = etag(result.revision)
    return {
        "level": result.level,
        "revision": revision_label(result.revision),
        "values": result.values,
    }


@router.patch(
    "",
    summary="Change settings at a level",
    responses=error_responses(403, 409, 412, 422),
    **route_kwargs("patch_settings"),
)
async def patch_settings(  # noqa: PLR0913 - body, level, flags, precondition and caller
    service: SettingsDependency,
    tenant: TenantDependency,
    principal: Annotated[Principal, Depends(require_role(OPERATIONS["patch_settings"].role))],
    body: Annotated[
        dict[str, Any], Body(description="Key to new value; null removes the override")
    ],
    response: Response,
    level: LevelQuery = None,
    dry_run: bool = False,
    reindex: bool = False,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    """Save the changes as the next revision; nothing is saved when any key is refused."""
    parsed = SettingsLevel.parse(level)
    if reindex:
        raise HarborValidationError(
            "reindex cannot be started from a settings change", details={"field": "reindex"}
        )
    expected = version_from_etag(if_match)
    if expected is None:
        raise HarborValidationError("If-Match is required", details={"field": "If-Match"})
    result = await service.patch(
        tenant_id=tenant,
        level=parsed,
        body=body,
        expected_revision=expected,
        dry_run=dry_run,
        actor=principal.subject,
    )
    response.headers["ETag"] = etag(result.revision)
    return {
        "revision": revision_label(result.revision),
        "dry_run": result.dry_run,
        "changed": [change.as_dict() for change in result.changed],
    }


@router.get(
    "/schema",
    summary="JSON schema of every setting",
    responses=error_responses(403),
    **route_kwargs("settings_schema"),
)
async def settings_schema(
    service: SettingsDependency,
    _principal: Annotated[Principal, Depends(require_role(OPERATIONS["settings_schema"].role))],
) -> dict[str, Any]:
    """The registry's settings by group: type, range, levels, when a change applies, description."""
    return service.schema()
