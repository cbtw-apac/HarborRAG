"""Admin-gated diagnostics route (ST9).

Returns composition diagnostics, composition mode, and a settings echo with
secrets redacted (harborrag_core.security.redaction as the last line of
defense on top of explicit field scrubbing).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.services.base import BaseAppService
from harborrag_core.security.redaction import redact_secrets

router = APIRouter(tags=["diagnostics"])


def _sweep_secrets(value: Any) -> Any:
    """Recursively apply redact_secrets to string leaves only.

    redact_secrets rewrites "label: value" text into "label=<redacted>",
    which is not JSON-safe -- running it over a json.dumps()'d blob (as this
    used to do) can turn a valid `"auth_secret":"<redacted>"` pair into
    `"auth_secret=<redacted>` and break re-parsing, since "secret" matches
    inside the key name itself. Sweeping only leaf string values keeps keys
    and JSON structure untouched.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _sweep_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sweep_secrets(item) for item in value]
    return value


def _redacted_settings_echo(settings: ApiSettings) -> dict[str, Any]:
    """Dump settings with secret fields scrubbed, then sweep every remaining
    string value through redact_secrets for any pattern-shaped stragglers."""
    echo = settings.model_dump()
    if echo.get("auth_secret"):
        echo["auth_secret"] = "<redacted>"
    swept: dict[str, Any] = _sweep_secrets(echo)
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
