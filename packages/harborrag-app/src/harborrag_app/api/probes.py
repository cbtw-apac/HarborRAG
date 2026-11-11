"""Liveness and readiness probes.

Handles:
- ``GET /healthz``: the process is up, with no dependency calls
- ``GET /readyz``: ``200`` when every dependency check is ``ok``, ``503`` otherwise

Does NOT authenticate, rate-limit or use the error envelope: probes answer the same
body whatever credential a caller sends.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from harborrag_runtime.config.settings import RuntimeSettings

from .readiness import ReadinessChecks, build_readiness_checks

router = APIRouter(tags=["Probes"])


class Liveness(BaseModel):
    """The process answers."""

    status: Literal["ok"]


class Readiness(BaseModel):
    """Every dependency check and the verdict they add up to."""

    status: Literal["ready", "not_ready"]
    checks: dict[str, Literal["ok", "fail"]]


def _readiness(request: Request) -> ReadinessChecks:
    """The app's readiness checks, built from the environment on first use."""
    app = request.app
    readiness: ReadinessChecks | None = getattr(app.state, "readiness", None)
    if readiness is None:

        async def temporal() -> bool:
            health = getattr(app.state.app_service, "runtime_health", None)
            return health is not None and (await health()).ok

        readiness = ReadinessChecks(
            build_readiness_checks(
                RuntimeSettings(),
                capacity_redis_url=app.state.settings.api_capacity_redis_url,
                temporal=temporal,
            )
        )
        app.state.readiness = readiness
    return readiness


@router.get("/healthz", summary="Liveness", response_model=Liveness)
def healthz() -> dict[str, str]:
    """Answer ``ok`` while the process serves requests."""
    return {"status": "ok"}


@router.get(
    "/readyz",
    summary="Readiness",
    response_model=Readiness,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Readiness}},
)
async def readyz(request: Request, response: Response) -> dict[str, object]:
    """Report each dependency; ``503`` when any of them fails."""
    checks = await _readiness(request).report()
    ready = all(result == "ok" for result in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not_ready", "checks": checks}
