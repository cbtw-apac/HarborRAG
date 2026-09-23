"""Read-side workspace settings endpoint (ML1/M1)."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.workflow_control import BaseAppService
from harborrag_core.security.redaction import redact_mapping

router = APIRouter(tags=["settings"], dependencies=[Depends(require_role("reader"))])


@router.get("/settings", response_model=dict[str, Any])
async def get_settings(
    service: Annotated[BaseAppService, Depends(get_app_service)],
) -> dict[str, Any]:
    """The workspace settings document, flat and redacted.

    The document is schemaless, so a webhook URL or a key can end up in it. It
    was returned verbatim to the lowest role that can reach this route, which
    made "reader" enough to read whatever anyone had stored.
    """

    response = await service.get_settings()
    return redact_mapping(cast(dict[str, Any], response.data["settings"].data))
