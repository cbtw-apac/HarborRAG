"""Read-side workspace settings endpoint (ML1/M1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.services.base import BaseAppService

router = APIRouter(tags=["settings"], dependencies=[Depends(require_role("reader"))])


class WorkspaceSettingsOut(BaseModel):
    data: dict[str, Any]


@router.get("/settings", response_model=WorkspaceSettingsOut)
async def get_settings(request: Request) -> WorkspaceSettingsOut:
    """The workspace settings document; an empty document if never written."""
    service: BaseAppService = request.app.state.app_service
    response = await service.get_settings()
    return WorkspaceSettingsOut(data=response.data["settings"].data)
