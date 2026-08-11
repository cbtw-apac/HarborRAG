"""Source endpoints: read side (ML1/M1) plus write side and catalog (ML2).

Secret-shaped config fields are extracted to the secrets port by the service
layer (workflow_control.control_plane.writes) -- routes never see or forward
raw values. Every route checks the caller's tenant against the source's
``tenant_id``; a source outside the caller's tenants 404s rather than 403s,
so its existence isn't leaked (mirrors api/v1/ingestion/routes.py).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, JsonValue, model_validator

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.schemas import ApiModel
from harborrag_app.workflow_control import BaseAppService
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.security.redaction import redact_mapping

router = APIRouter(tags=["sources"], dependencies=[Depends(require_role("reader"))])


class SourceCreateInput(ApiModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=255)
    source_type: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    config: dict[str, JsonValue] = Field(default_factory=dict)
    schedule: str | None = None


class SourceUpdateInput(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: dict[str, JsonValue] | None = None
    schedule: str | None = None
    status: str | None = None

    @model_validator(mode="after")
    def reject_explicit_null_config(self) -> SourceUpdateInput:
        """`{"config": null}` must 422, not crash the secrets-extraction path."""
        if "config" in self.model_fields_set and self.config is None:
            raise ValueError("config must be omitted, not null; send {} to clear it")
        return self


class SourceOut(BaseModel):
    id: str
    tenant_id: str
    project_id: str
    source_type: str
    name: str
    config: dict[str, Any]
    secret_refs: list[str]
    schedule: str | None
    status: str

    @classmethod
    def from_domain(cls, source: SourceConfig) -> SourceOut:
        return cls(
            id=source.id,
            tenant_id=source.tenant_id,
            project_id=source.project_id,
            source_type=source.source_type,
            name=source.name,
            config=redact_mapping(source.config),
            secret_refs=source.secret_refs,
            schedule=source.schedule,
            status=source.status,
        )


async def _authorized_source(
    service: BaseAppService,
    source_id: str,
    principal: Principal,
) -> SourceConfig:
    """One source by id, 404 (not 403) when it exists but is outside the caller's tenants."""
    response = await service.get_source(source_id)
    source: SourceConfig = response.data["source"]
    if not principal.can_access_tenant(source.tenant_id):
        raise HarborNotFoundError(f"source {source_id!r} not found")
    return source


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("reader"))],
    project_id: str | None = None,
) -> list[SourceOut]:
    """Sources, optionally filtered to one project, scoped to the caller's tenants."""
    response = await service.list_sources(project_id)
    return [
        SourceOut.from_domain(source)
        for source in response.data["sources"]
        if principal.can_access_tenant(source.tenant_id)
    ]


@router.get("/sources/{source_id}", response_model=SourceOut)
async def get_source(
    source_id: str,
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> SourceOut:
    """One source by id; 404 (enveloped) when missing or outside the caller's tenants."""
    return SourceOut.from_domain(await _authorized_source(service, source_id, principal))


@router.post("/sources", status_code=201, response_model=SourceOut)
async def create_source(
    payload: SourceCreateInput,
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> SourceOut:
    """Create a source; secret-shaped config fields never round-trip in the response."""
    authorize_tenant(principal, payload.tenant_id)
    response = await service.create_source(
        tenant_id=payload.tenant_id,
        project_id=payload.project_id,
        source_type=payload.source_type,
        name=payload.name,
        config=payload.config,
        schedule=payload.schedule,
        actor=principal.subject,
    )
    return SourceOut.from_domain(response.data["source"])


@router.patch("/sources/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: str,
    payload: SourceUpdateInput,
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> SourceOut:
    """Update a source; fields omitted from the request body are left unchanged."""
    await _authorized_source(service, source_id, principal)
    response = await service.update_source(
        source_id,
        updates=payload.model_dump(exclude_unset=True),
        actor=principal.subject,
    )
    return SourceOut.from_domain(response.data["source"])


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: str,
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> None:
    """Delete a source and forget every secret it referenced."""
    await _authorized_source(service, source_id, principal)
    await service.delete_source(source_id, actor=principal.subject)
