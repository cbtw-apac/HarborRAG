"""Source -> Job bridge (ML2 P2/P3): persists a Job row, drives it through the
existing Temporal ingestion path (start_ingestion/ingestion_status/
ingestion_result/control_ingestion, all defined directly on AppService), and
bridges Temporal's pull-only status into the event bus for streaming (P3).

Job.status transitions: "queued" (create), "running"/"failed" (create's
start_ingestion outcome), "cancelled" (a successful control_job("cancel")),
and "succeeded"/"failed" (sync_job_progress, reconciling Temporal's actual
terminal execution_status -- see module docstring on sync_job_progress).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborConflictError,
    HarborNotFoundError,
)
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.job import Job, JobCounters, JobStatus, JobType
from harborrag_core.ports.events import EventBusPort
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.connectors.providers import canonical_provider_name, config_factory

from .schemas import AppResponse
from .writes import _log_activity

# Temporal's execution_status (temporal/client.py) mapped to the terminal
# JobStatus values sync_job_progress reconciles to; "running"/
# "continued_as_new"/"unknown" are deliberately absent -- not terminal, keep
# polling.
_TERMINAL_EXECUTION_STATUS: dict[str, JobStatus] = {
    "completed": "succeeded",
    "canceled": "cancelled",
    "failed": "failed",
    "terminated": "failed",
    "timed_out": "failed",
}

# Fields compared tick-to-tick to decide whether a job's progress changed
# enough to publish + persist a new event.
_PROGRESS_SNAPSHOT_FIELDS = (
    "execution_status",
    "status",
    "progress",
    "failed_artifacts",
    "quarantined_artifacts",
    "pending_resolutions",
)


class JobsMixin:
    """Job use cases shared by AppService; calls both the control plane and
    the Temporal ingestion methods AppService defines directly."""

    def _control_plane(self) -> ControlPlaneRepositories:
        raise NotImplementedError

    def _event_bus(self) -> EventBusPort:
        raise NotImplementedError

    async def start_ingestion(
        self,
        *,
        tenant_id: str,
        connector_name: str,
        run_id: str | None = None,
        manifest_id: str | None = None,
        generation_id: str | None = None,
        max_artifacts: int | None = None,
        wait: bool = False,
    ) -> AppResponse:
        raise NotImplementedError

    async def ingestion_status(self, run_id: str) -> AppResponse:
        raise NotImplementedError

    async def ingestion_result(self, run_id: str) -> AppResponse:
        raise NotImplementedError

    async def control_ingestion(
        self,
        run_id: str,
        action: str,
        *,
        artifact_ids: tuple[str, ...] = (),
        graceful: bool = True,
    ) -> AppResponse:
        raise NotImplementedError

    async def create_job(
        self,
        source_id: str,
        *,
        job_type: JobType = "bulk_ingest",
        dry_run: bool = False,
        run_id: str | None = None,
        manifest_id: str | None = None,
        generation_id: str | None = None,
        max_artifacts: int | None = None,
        wait: bool = False,
        actor: str,
    ) -> AppResponse:
        """Create a Job for a source and start it on the Temporal path.

        data={"job": Job, "run": ..., "workflow": ..., ["result": ...]} on
        success; on a Temporal-side failure, data still carries "job" (now
        persisted as status="failed") alongside the usual "error_type".
        """
        control_plane = self._control_plane()
        source = await control_plane.sources.get(source_id)
        if source is None:
            raise HarborNotFoundError(f"source {source_id!r} not found")
        connector_name = canonical_provider_name(source.source_type)
        if config_factory(connector_name) is None:
            raise HarborCapabilityError(f"source_type {source.source_type!r} is not supported")

        job_id = run_id or f"job_{uuid4().hex}"
        if await control_plane.jobs.get(job_id) is not None:
            raise HarborConflictError(f"job {job_id!r} already exists")

        job = Job(
            id=job_id,
            source_id=source_id,
            project_id=source.project_id,
            job_type=job_type,
            dry_run=dry_run,
            payload={"connector_name": connector_name},
        )
        await control_plane.jobs.save(job)

        ingestion_response = await self.start_ingestion(
            tenant_id=source.project_id,
            connector_name=connector_name,
            run_id=job.id,
            manifest_id=manifest_id,
            generation_id=generation_id,
            max_artifacts=max_artifacts,
            wait=wait,
        )
        if ingestion_response.ok:
            job.status = "running"
            await control_plane.jobs.save(job)
            await _log_activity(
                control_plane,
                actor,
                "created",
                "job",
                job.id,
                f"Triggered {job_type} job for source {source.name!r}",
            )
            return AppResponse(True, {"job": job, **ingestion_response.data})

        job.status = "failed"
        job.last_error = ingestion_response.error
        await control_plane.jobs.save(job)
        await _log_activity(
            control_plane,
            actor,
            "failed",
            "job",
            job.id,
            f"Failed to start {job_type} job for source {source.name!r}",
        )
        return AppResponse(
            False,
            {"job": job, **ingestion_response.data},
            ingestion_response.error,
        )

    async def list_jobs(
        self,
        *,
        source_id: str | None = None,
        status: JobStatus | None = None,
    ) -> AppResponse:
        """Jobs filtered by source and/or status; data={"jobs": [Job, ...]}."""
        jobs = await self._control_plane().jobs.list(status=status, source_id=source_id)
        return AppResponse(True, {"jobs": jobs})

    async def get_job(self, job_id: str) -> AppResponse:
        """One job merged with its live Temporal state.

        data={"job": Job, "live": {...}} on success; on a Temporal-side
        failure (e.g. the run genuinely isn't known to Temporal), data still
        carries "job" alongside the usual "error_type".
        """
        job = await self._control_plane().jobs.get(job_id)
        if job is None:
            raise HarborNotFoundError(f"job {job_id!r} not found")
        live = await self.ingestion_status(job_id)
        if live.ok:
            return AppResponse(True, {"job": job, "live": live.data})
        return AppResponse(False, {"job": job, **live.data}, live.error)

    async def get_job_result(self, job_id: str) -> AppResponse:
        """One job merged with its terminal Temporal result; data={"job", "result"}."""
        job = await self._control_plane().jobs.get(job_id)
        if job is None:
            raise HarborNotFoundError(f"job {job_id!r} not found")
        result = await self.ingestion_result(job_id)
        if result.ok:
            return AppResponse(True, {"job": job, **result.data})
        return AppResponse(False, {"job": job, **result.data}, result.error)

    async def control_job(
        self,
        job_id: str,
        action: str,
        *,
        artifact_ids: tuple[str, ...] = (),
        graceful: bool = True,
        actor: str,
    ) -> AppResponse:
        """Pause/resume/cancel/retry a job's run; data={"job", "action", "artifact_ids"}."""
        control_plane = self._control_plane()
        job = await control_plane.jobs.get(job_id)
        if job is None:
            raise HarborNotFoundError(f"job {job_id!r} not found")
        response = await self.control_ingestion(
            job_id,
            action,
            artifact_ids=artifact_ids,
            graceful=graceful,
        )
        if not response.ok:
            return AppResponse(False, {"job": job, **response.data}, response.error)
        if action == "cancel":
            job.status = "cancelled"
            await control_plane.jobs.save(job)
        await _log_activity(
            control_plane,
            actor,
            action,
            "job",
            job_id,
            f"Applied {action!r} to job {job_id!r}",
        )
        return AppResponse(True, {"job": job, **response.data})

    async def sync_job_progress(self) -> AppResponse:
        """One poll tick: diff every running job's live Temporal state.

        For each job whose live snapshot changed since the last tick, publish
        + persist a "job.<id>.progress" event. When Temporal's execution_status
        reaches a terminal value, reconcile Job.status and publish/persist a
        final "job.<id>.done" event too -- the stream route's stop signal.
        Called once per tick by workflow_control.progress_bridge; data=
        {"synced": <jobs examined>}.
        """
        control_plane = self._control_plane()
        event_bus = self._event_bus()
        running = await control_plane.jobs.list(status="running")
        for job in running:
            live = await self.ingestion_status(job.id)
            if not live.ok:
                continue

            snapshot = {field: live.data.get(field) for field in _PROGRESS_SNAPSHOT_FIELDS}
            if snapshot != job.payload.get("_last_progress"):
                progress = live.data.get("progress") or {}
                # chunks_created has no source here: Temporal's get_progress query
                # (RunProgress) tracks artifact-level counts only, not chunks.
                job.counters = JobCounters(
                    documents_processed=progress.get("processed", job.counters.documents_processed),
                    chunks_created=job.counters.chunks_created,
                    errors=progress.get("failed", job.counters.errors),
                )
                event = HarborEvent(
                    name=f"job.{job.id}.progress", trace_id=job.id, payload=live.data
                )
                await event_bus.publish(event)
                await control_plane.jobs.append_event(job.id, event)
                job.payload["_last_progress"] = snapshot
                await control_plane.jobs.save(job)

            terminal_status = _TERMINAL_EXECUTION_STATUS.get(str(live.data.get("execution_status")))
            if terminal_status is not None:
                job.status = terminal_status
                await control_plane.jobs.save(job)
                done_event = HarborEvent(
                    name=f"job.{job.id}.done", trace_id=job.id, payload=live.data
                )
                await event_bus.publish(done_event)
                await control_plane.jobs.append_event(job.id, done_event)
        return AppResponse(True, {"synced": len(running)})

    async def stream_job_events(self, job_id: str) -> AsyncIterator[HarborEvent]:
        """Backlog replay then a live tail of a job's events.

        Subscribes before reading the backlog so nothing published in the gap
        between the two is ever dropped (a harmless duplicate at worst, since
        every payload is a full snapshot). Stops after a "job.<id>.done"
        event -- or immediately after the backlog if the job's persisted
        status is already terminal, since sync_job_progress only ever touches
        "running" jobs and a job that failed synchronously in create_job (or
        was cancelled via control_job) never gets a live ".done" event.
        """
        control_plane = self._control_plane()
        job = await control_plane.jobs.get(job_id)
        if job is None:
            raise HarborNotFoundError(f"job {job_id!r} not found")
        if job.status in ("succeeded", "failed", "cancelled"):
            for event in await control_plane.jobs.list_events(job_id):
                yield event
            return
        live = self._event_bus().subscribe(f"job.{job_id}.")
        for event in await control_plane.jobs.list_events(job_id):
            yield event
        async for event in live:
            yield event
            if event.name.endswith(".done"):
                return
