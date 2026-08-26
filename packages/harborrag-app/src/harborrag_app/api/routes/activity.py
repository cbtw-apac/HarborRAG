"""Read-side activity (audit feed) endpoint (ML1/M1)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.workflow_control import BaseAppService
from harborrag_core.domain.activity import ActivityEntry

router = APIRouter(tags=["activity"], dependencies=[Depends(require_role("reader"))])


class ActivityEntryOut(BaseModel):
    id: str
    actor: str
    verb: str
    entity_type: str
    entity_id: str
    summary: str
    created_at: datetime

    @classmethod
    def from_domain(cls, entry: ActivityEntry) -> ActivityEntryOut:
        return cls(
            id=entry.id,
            actor=entry.actor,
            verb=entry.verb,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            summary=entry.summary,
            created_at=entry.created_at,
        )


@router.get("/activity", response_model=list[ActivityEntryOut])
async def list_activity(
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("reader"))],
    limit: int = Query(50, ge=1, le=200),
) -> list[ActivityEntryOut]:
    """Most recent audit entries within the caller's tenants, newest first."""
    response = await service.list_activity(limit, tenant_ids=principal.tenant_scope)
    return [ActivityEntryOut.from_domain(entry) for entry in response.data["activity"]]
