"""Prometheus scrape endpoint for API and process observations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.metrics import ApiMetrics

router = APIRouter(
    tags=["observability"],
    dependencies=[Depends(require_role("admin"))],
)


@router.get("/metrics", response_class=Response)
def metrics(request: Request) -> Response:
    """Return this API process's Prometheus exposition document."""
    registry: ApiMetrics = request.app.state.api_metrics
    return Response(
        content=registry.render(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )
