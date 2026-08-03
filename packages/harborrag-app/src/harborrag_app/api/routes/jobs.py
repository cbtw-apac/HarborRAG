"""Job endpoints (ML2 P2): trigger an ingestion job for a source, and track it.

Wraps the same Temporal ingestion path /ingestions already exposes, bridged
through a persisted Job row keyed by source_id -- see
workflow_control.jobs.JobsMixin for the create/status/result/action logic.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse

from harborrag_app.api.auth.dependencies import require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.dependencies import get_app_service
from harborrag_app.api.routes._ingestion_rendering import render_ingestion_response
from harborrag_app.api.schemas import HarborAPISchema, IngestionControlInput
from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.job import Job, JobCounters, JobStatus, JobType

router = APIRouter(tags=["jobs"], dependencies=[Depends(require_role("reader"))])


class JobCreateInput(HarborAPISchema):
    job_type: JobType = "bulk_ingest"
    dry_run: bool = False
    run_id: str | None = Field(default=None, min_length=1, max_length=512)
    manifest_id: str | None = Field(default=None, min_length=1, max_length=512)
    generation_id: str | None = Field(default=None, min_length=1, max_length=512)
    max_artifacts: int | None = Field(default=None, ge=1)
    wait: bool = False


class JobCountersOut(BaseModel):
    documents_processed: int
    chunks_created: int
    errors: int

    @classmethod
    def from_domain(cls, counters: JobCounters) -> JobCountersOut:
        return cls(
            documents_processed=counters.documents_processed,
            chunks_created=counters.chunks_created,
            errors=counters.errors,
        )


class JobOut(BaseModel):
    id: str
    source_id: str
    project_id: str
    job_type: JobType
    status: JobStatus
    dry_run: bool
    attempts: int
    enqueued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    counters: JobCountersOut
    last_error: str | None

    @classmethod
    def from_domain(cls, job: Job) -> JobOut:
        return cls(
            id=job.id,
            source_id=job.source_id,
            project_id=job.project_id,
            job_type=job.job_type,
            status=job.status,
            dry_run=job.dry_run,
            attempts=job.attempts,
            enqueued_at=job.enqueued_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            counters=JobCountersOut.from_domain(job.counters),
            last_error=job.last_error,
        )


def _rendered(response: AppResponse) -> AppResponse:
    """Serialize the raw Job dataclass under "job" so JSONResponse can encode it."""
    if "job" not in response.data:
        return response
    data = {
        **response.data,
        "job": JobOut.from_domain(response.data["job"]).model_dump(mode="json"),
    }
    return AppResponse(response.ok, data, response.error)


@router.post("/sources/{source_id}/jobs", status_code=202)
async def create_job(
    source_id: str,
    payload: JobCreateInput,
    request: Request,
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> JSONResponse:
    """Create a job for a source and start it on the Temporal ingestion path."""
    response = await service.create_job(
        source_id,
        job_type=payload.job_type,
        dry_run=payload.dry_run,
        run_id=payload.run_id,
        manifest_id=payload.manifest_id,
        generation_id=payload.generation_id,
        max_artifacts=payload.max_artifacts,
        wait=payload.wait,
        actor=principal.subject,
    )
    status_code = 200 if payload.wait else 202
    return render_ingestion_response(
        request,
        _rendered(response),
        operation="create_job",
        success_status=status_code,
    )


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    service: Annotated[BaseAppService, Depends(get_app_service)],
    source_id: str | None = None,
    status: JobStatus | None = None,
) -> list[JobOut]:
    """Jobs, optionally filtered by source and/or status."""
    response = await service.list_jobs(source_id=source_id, status=status)
    return [JobOut.from_domain(job) for job in response.data["jobs"]]


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    request: Request,
    service: Annotated[BaseAppService, Depends(get_app_service)],
) -> JSONResponse:
    """One job's persisted row merged with its live Temporal state."""
    response = await service.get_job(job_id)
    return render_ingestion_response(request, _rendered(response), operation="get_job")


@router.get("/jobs/{job_id}/result")
async def get_job_result(
    job_id: str,
    request: Request,
    service: Annotated[BaseAppService, Depends(get_app_service)],
) -> JSONResponse:
    """One job's persisted row merged with its terminal Temporal result."""
    response = await service.get_job_result(job_id)
    return render_ingestion_response(request, _rendered(response), operation="get_job_result")


@router.get("/jobs/{job_id}/stream")
async def stream_job(
    job_id: str,
    service: Annotated[BaseAppService, Depends(get_app_service)],
) -> EventSourceResponse:
    """Backlog replay then a live tail of a job's progress events (SSE).

    Pumps the first event outside of EventSourceResponse: a missing job's
    HarborNotFoundError must raise here, in the plain route coroutine, so
    FastAPI's normal exception handling turns it into the usual enveloped
    404 -- once EventSourceResponse starts streaming, the HTTP status is
    already committed and an error can no longer change it.
    """
    events = service.stream_job_events(job_id)
    try:
        first_event = await events.__anext__()
    except StopAsyncIteration:

        async def _empty() -> AsyncIterator[dict[str, str]]:
            return
            yield  # pragma: no cover - unreachable, makes this an async generator

        return EventSourceResponse(_empty())

    async def _frames() -> AsyncIterator[dict[str, str]]:
        yield _frame(first_event)
        async for event in events:
            yield _frame(event)

    return EventSourceResponse(_frames())


def _frame(event: HarborEvent) -> dict[str, str]:
    return {"event": event.name, "data": json.dumps(event.payload, default=str)}


@router.post("/jobs/{job_id}/actions")
async def control_job(
    job_id: str,
    payload: IngestionControlInput,
    request: Request,
    service: Annotated[BaseAppService, Depends(get_app_service)],
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> JSONResponse:
    """Pause, resume, cancel, or retry artifacts in a job's run."""
    response = await service.control_job(
        job_id,
        payload.action,
        artifact_ids=tuple(payload.artifact_ids),
        graceful=payload.graceful,
        actor=principal.subject,
    )
    return render_ingestion_response(request, _rendered(response), operation=payload.action)
