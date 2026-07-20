"""Admin-gated diagnostics route (ST9).

Returns composition diagnostics, composition mode, and a settings echo with
secrets redacted (harborrag_core.security.redaction as the last line of
defense on top of explicit field scrubbing).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from harborrag_core.security.redaction import redact_secrets

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.services.base import BaseAppService

router = APIRouter(tags=["diagnostics"])


def _redacted_settings_echo(settings: ApiSettings) -> dict[str, Any]:
    """Dump settings with secret fields scrubbed, then sweep the rendered
    JSON through redact_secrets for any pattern-shaped stragglers."""
    echo = settings.model_dump()
    if echo.get("auth_secret"):
        echo["auth_secret"] = "<redacted>"
    swept: dict[str, Any] = json.loads(redact_secrets(json.dumps(echo)))
    return swept


@router.get("/diagnostics")
def diagnostics(
    request: Request,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict[str, object]:
    """Full composition diagnostics; requires the admin role (ST4 proof)."""
    service: BaseAppService = request.app.state.app_service
    settings: ApiSettings = request.app.state.settings
    health = service.health()
    return {
        "ok": health.ok,
        "diagnostics": health.data,
        "composition_mode": getattr(request.app.state, "composition_mode", "unknown"),
        "settings": _redacted_settings_echo(settings),
        "principal": {"subject": principal.subject, "role": principal.role},
    }
