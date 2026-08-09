"""Read-side source endpoints (ML1/M1).

Thin HTTP wrapper over BaseAppService.{list_sources,get_source}; write
operations (create/update/delete, secrets handling) land with ML2.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.workflow_control import BaseAppService
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.security.redaction import redact_mapping

router = APIRouter(tags=["sources"], dependencies=[Depends(require_role("reader"))])


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
