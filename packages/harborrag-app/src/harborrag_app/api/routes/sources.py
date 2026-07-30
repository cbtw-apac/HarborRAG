"""Source endpoints: read side (ML1/M1) plus write side and catalog (ML2).

Secret-shaped config fields are extracted to the secrets port by the service
layer (workflow_control.writes) -- routes never see or forward raw values.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, JsonValue

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.schemas import HarborAPISchema
from harborrag_app.workflow_control import BaseAppService
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.security.redaction import redact_mapping
from harborrag_runtime.config.connectors.providers import (
    SECRET_CONFIG_FIELDS,
    config_factory,
    config_field_names,
    supported_provider_names,
)

router = APIRouter(tags=["sources"], dependencies=[Depends(require_role("reader"))])


class SourceCreateInput(HarborAPISchema):
    project_id: str = Field(min_length=1, max_length=255)
    source_type: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    config: dict[str, JsonValue] = Field(default_factory=dict)
    schedule: str | None = None


class SourceUpdateInput(HarborAPISchema):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: dict[str, JsonValue] | None = None
    schedule: str | None = None
    status: str | None = None


class SourceTypeOut(BaseModel):
    source_type: str
    fields: list[str]
    secret_fields: list[str]


class SourceOut(BaseModel):
    id: str
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
            project_id=source.project_id,
            source_type=source.source_type,
            name=source.name,
            config=redact_mapping(source.config),
            secret_refs=source.secret_refs,
            schedule=source.schedule,
            status=source.status,
        )


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(
    service: Annotated[BaseAppService, Depends(get_app_service)],
    project_id: str | None = None,
) -> list[SourceOut]:
    """Sources, optionally filtered to one project."""
    response = await service.list_sources(project_id)
    return [SourceOut.from_domain(source) for source in response.data["sources"]]


@router.get("/sources/{source_id}", response_model=SourceOut)
async def get_source(
    source_id: str,
    service: Annotated[BaseAppService, Depends(get_app_service)],
) -> SourceOut:
    """One source by id; 404 (enveloped) when it does not exist."""
    response = await service.get_source(source_id)
    return SourceOut.from_domain(response.data["source"])


@router.get("/source-types", response_model=list[SourceTypeOut])
async def list_source_types() -> list[SourceTypeOut]:
    """Supported source types and their config fields, for wizard forms."""
    types: list[SourceTypeOut] = []
    for name in supported_provider_names():
        factory = config_factory(name)
        assert factory is not None  # supported_provider_names() only lists known factories
        fields = config_field_names(factory)
        types.append(
            SourceTypeOut(
                source_type=name,
                fields=sorted(fields),
                secret_fields=sorted(SECRET_CONFIG_FIELDS & fields),
            )
        )
    return types


@router.post("/sources", status_code=201, response_model=SourceOut)
async def create_source(
    payload: SourceCreateInput,
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> SourceOut:
    """Create a source; secret-shaped config fields never round-trip in the response."""
    response = await service.create_source(
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
    await service.delete_source(source_id, actor=principal.subject)
