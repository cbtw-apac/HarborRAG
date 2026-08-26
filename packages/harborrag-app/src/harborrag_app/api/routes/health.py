"""Dependency-free process liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    """Liveness: the process is up; never blocks on dependencies."""
    return {"status": "ok", "version": request.app.version}


@router.get(
    "/readyz",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Runtime not ready"}},
)
def readiness(request: Request, response: Response) -> dict[str, object]:
    """Readiness: the composed control plane can safely serve requests."""

    result = request.app.state.app_service.health()
    if not result.ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if result.ok else "not_ready",
        "version": request.app.version,
    }
