"""Liveness and readiness probes (ST9).

/health answers without touching any dependency; /readyz consults the app
service (control DB ping + migrations once ST8 wires production composition).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from harborrag_app.api.errors import error_envelope
from harborrag_app.services.base import BaseAppService

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    """Liveness: the process is up; never blocks on dependencies."""
    return {"status": "ok", "version": request.app.version}


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    """Readiness: 200 when the composed app service reports healthy,
    503 envelope otherwise (compose/Kubernetes probe target)."""
    service: BaseAppService = request.app.state.app_service
    response = service.health()
    if response.ok:
        return JSONResponse(status_code=200, content={"status": "ready", "data": response.data})
    return JSONResponse(
        status_code=503,
        content=error_envelope(request, "not_ready", response.error or "service not ready", {}),
    )
