"""Read-side workspace settings endpoint (ML1/M1)."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.workflow_control import BaseAppService

router = APIRouter(tags=["settings"], dependencies=[Depends(require_role("reader"))])


@router.get("/settings", response_model=dict[str, Any])
async def get_settings(
    service: Annotated[BaseAppService, Depends(get_app_service)],
) -> dict[str, Any]:
    """The workspace settings document, flat; an empty document if never written."""
    response = await service.get_settings()
    # TODO(ML4): redact settings via harborrag_core.security.redaction.redact_mapping
    # before returning if documents may contain webhook URLs or keys.
    return cast(dict[str, Any], response.data["settings"].data)
