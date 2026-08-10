"""Read-side project endpoints (ML1/M1).

Thin HTTP wrapper over BaseAppService.{list_projects,get_project}; the
service already resolves to the control-plane DB in production and to an
empty FakeProjectRepository in dev/mock mode.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.workflow_control import BaseAppService
from harborrag_core.domain.project import Project

router = APIRouter(tags=["projects"], dependencies=[Depends(require_role("reader"))])


class ProjectStatsOut(BaseModel):
    documents: int
    chunks: int
    size_bytes: int
    last_sync_at: datetime | None


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str
    collection: str
    status: str
    created_at: datetime
    updated_at: datetime
    stats: ProjectStatsOut

    @classmethod
    def from_domain(cls, project: Project) -> ProjectOut:
        return cls(
            id=project.id,
            name=project.name,
            description=project.description,
            collection=project.collection,
            status=project.status,
            created_at=project.created_at,
            updated_at=project.updated_at,
            stats=ProjectStatsOut(
                documents=project.stats.documents,
                chunks=project.stats.chunks,
                size_bytes=project.stats.size_bytes,
                last_sync_at=project.stats.last_sync_at,
            ),
        )


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> list[ProjectOut]:
    """Projects visible to the caller's tenants, unpaginated (pagination is an open ML1 decision)."""
    response = await service.list_projects(tenant_ids=principal.tenant_scope)
    return [ProjectOut.from_domain(project) for project in response.data["projects"]]


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: str,
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> ProjectOut:
    """One project by id; 404 (enveloped) when missing or outside the caller's tenants."""
    response = await service.get_project(project_id, tenant_ids=principal.tenant_scope)
    return ProjectOut.from_domain(response.data["project"])
