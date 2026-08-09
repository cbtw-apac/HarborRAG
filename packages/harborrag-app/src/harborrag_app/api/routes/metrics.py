"""Prometheus process metrics and read-side ingestion counters."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.metrics import ApiMetrics
from harborrag_app.workflow_control import BaseAppService

router = APIRouter(tags=["metrics"])


class JobsByStatusOut(BaseModel):
    queued: int
    running: int
    succeeded: int
    failed: int
    cancelled: int


class MetricsOut(BaseModel):
    projects_total: int
    sources_total: int
    documents_total: int
    chunks_total: int
    jobs_by_status: JobsByStatusOut


@router.get(
    "/metrics",
    response_class=Response,
    dependencies=[Depends(require_role("admin"))],
)
def metrics(request: Request) -> Response:
    """Return this API process's Prometheus exposition document."""
    registry: ApiMetrics = request.app.state.api_metrics
    return Response(
        content=registry.render(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


@router.get(
    "/metrics/ingestion",
    response_model=MetricsOut,
    dependencies=[Depends(require_role("reader"))],
)
async def get_metrics(service: Annotated[BaseAppService, Depends(get_app_service)]) -> MetricsOut:
    """Dashboard summary counters; all zero on a fresh workspace."""
    response = await service.get_metrics()
    return MetricsOut(**response.data)
