"""Read-side dashboard metrics endpoint (ML1/M1).

See harborrag_app.workflow_control.metrics for what these counters are derived from
and why there is no dedicated metrics table yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.workflow_control import BaseAppService

router = APIRouter(tags=["metrics"], dependencies=[Depends(require_role("reader"))])


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


@router.get("/metrics", response_model=MetricsOut)
async def get_metrics(request: Request) -> MetricsOut:
    """Dashboard summary counters; all zero on a fresh workspace."""
    service: BaseAppService = request.app.state.app_service
    response = await service.get_metrics()
    return MetricsOut(**response.data)
