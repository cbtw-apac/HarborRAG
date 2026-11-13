"""`/v1/admin/settings[/schema]`: read, patch and introspect runtime setting overrides.

Handles:
- ``GET /v1/admin/settings/schema`` (editor): the full key catalog, for a settings UI
- ``GET /v1/admin/settings?level=`` (admin; editor for ``connection:`` levels): raw or
  effective values at one scope
- ``PATCH /v1/admin/settings?level=`` (admin): upsert or remove overrides, ``If-Match``
  required, optimistic-locked on the scope's revision

Routes translate HTTP to ``SettingsService``: the tenant from the credential or
``X-Tenant-Id``, ``level`` into ``(scope_level, scope_id)``, ``ETag``/``If-Match``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response

from harborrag_app.api.auth.access import require_role
from harborrag_app.api.auth.dependencies import authorize_role
from harborrag_app.api.auth.tenancy import TenantDependency
from harborrag_app.workflow_control.control_plane.connection_views import etag, version_from_etag
from harborrag_app.workflow_control.control_plane.settings import parse_level
from harborrag_core.domain.setting_registry import settings_schema_groups

from .dependencies import Admin, Administration, Editor, error_responses
from .settings_schemas import (
    EffectiveValueOut,
    SettingGroupOut,
    SettingsChangeOut,
    SettingsChangeOutEnvelope,
    SettingSchemaOut,
    SettingsEffectiveReadOut,
    SettingsReadOut,
    SettingsSchemaOut,
)

_errors = error_responses

LevelParam = Annotated[
    str,
    Query(
        description="tenant, project:{project_id} or connection:{project_id}/{connection_id}",
        examples=["tenant", "project:cpm", "connection:cpm/cpm-tickets"],
    ),
]
IfMatchRequired = Annotated[
    str,
    Header(
        alias="If-Match",
        description="ETag from your last read; the write fails with 412 if someone changed it since",
    ),
]

settings_editor_router = APIRouter(
    prefix="/admin",
    tags=["Settings"],
    dependencies=[Depends(require_role("editor"))],
)
settings_admin_router = APIRouter(
    prefix="/admin",
    tags=["Settings"],
    dependencies=[Depends(require_role("admin"))],
)


def _revision_str(revision: int) -> str:
    return f"rev_{revision}"


@settings_editor_router.get(
    "/settings/schema",
    summary="Setting schema",
    response_model=SettingsSchemaOut,
    response_model_exclude_none=True,
)
async def get_settings_schema() -> SettingsSchemaOut:
    """Every runtime setting with its type, default, allowed levels and apply timing."""
    groups = [
        SettingGroupOut(
            name=group.name,
            title=group.title,
            settings=[
                SettingSchemaOut(
                    key=spec.key,
                    type=spec.type,
                    default=spec.default,
                    min=spec.minimum,
                    max=spec.maximum,
                    enum=spec.enum,
                    items_type=spec.items_type,
                    levels=sorted(spec.levels),
                    applies=spec.applies,
                    description=spec.description,
                )
                for spec in group.settings
            ],
        )
        for group in settings_schema_groups()
    ]
    return SettingsSchemaOut(groups=groups)


@settings_editor_router.get(
    "/settings",
    summary="Read settings",
    responses=_errors(403, 422),
)
async def get_settings(
    service: Administration,
    tenant: TenantDependency,
    principal: Editor,
    response: Response,
    level: LevelParam = "tenant",
    effective: Annotated[
        bool, Query(description="Merge all levels and show where each value comes from")
    ] = False,
) -> dict[str, Any]:
    """Editors may read ``level=connection:...`` for connections in their projects.

    Everything else (``tenant`` and ``project:...``) requires ``admin``.
    """
    scope_level, scope_id = parse_level(level)
    if scope_level != "connection":
        authorize_role(principal, "admin")
    result = await service.settings_service.read(tenant, scope_level, scope_id, effective=effective)
    response.headers["ETag"] = etag(result.revision)
    if effective:
        return SettingsEffectiveReadOut(
            level=result.level,
            revision=_revision_str(result.revision),
            values={
                key: EffectiveValueOut(value=v["value"], origin=v["origin"])
                for key, v in result.values.items()
            },
        ).model_dump()
    return SettingsReadOut(
        level=result.level, revision=_revision_str(result.revision), values=result.values
    ).model_dump()


@settings_admin_router.patch(
    "/settings",
    summary="Change settings",
    response_model=SettingsChangeOutEnvelope,
    responses=_errors(409, 412, 422),
)
async def patch_settings(
    service: Administration,
    tenant: TenantDependency,
    principal: Admin,
    body: Annotated[
        dict[str, Any], Body(description="Setting key -> new value, or null to remove")
    ],
    response: Response,
    if_match: IfMatchRequired,
    level: LevelParam = "tenant",
    dry_run: Annotated[bool, Query(description="Preview only")] = False,
) -> SettingsChangeOutEnvelope:
    """``live`` changes apply within seconds; ``next_ingestion`` changes apply to future runs."""
    scope_level, scope_id = parse_level(level)
    result = await service.settings_service.patch(
        tenant,
        scope_level,
        scope_id,
        body,
        expected_revision=version_from_etag(if_match) or 0,
        actor=principal.subject,
        dry_run=dry_run,
    )
    response.headers["ETag"] = etag(result.revision)
    return SettingsChangeOutEnvelope(
        revision=_revision_str(result.revision),
        dry_run=result.dry_run,
        changed=[
            SettingsChangeOut.model_validate(
                {"key": c.key, "from": c.before, "to": c.after, "applies": c.applies}
            )
            for c in result.changed
        ],
    )


routers = (settings_editor_router, settings_admin_router)
