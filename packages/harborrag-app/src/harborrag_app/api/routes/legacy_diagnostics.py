"""Deprecated, admin-gated diagnostics compatibility route."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import SecretStr

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control import BaseAppService
from harborrag_core.security.redaction import redact_secrets

router = APIRouter(tags=["diagnostics"])

_DEPRECATION_HEADERS = {
    "Deprecation": "true",
    "Sunset": "Sat, 07 Feb 2027 00:00:00 GMT",
    "Link": '</api/v1/readyz>; rel="successor-version"',
}


def _sweep_secrets(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return "<redacted>"
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _sweep_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sweep_secrets(item) for item in value]
    return value


def _redacted_settings_echo(settings: ApiSettings) -> dict[str, Any]:
    return {key: _sweep_secrets(value) for key, value in settings.model_dump().items()}


@router.get("/diagnostics", deprecated=True)
def diagnostics(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict[str, object]:
    """Return redacted legacy diagnostics during the deprecation window."""

    response.headers.update(_DEPRECATION_HEADERS)
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
