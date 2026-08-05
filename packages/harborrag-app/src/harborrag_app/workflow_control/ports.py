from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.job import JobStatus, JobType

from .schemas import AppResponse, JobRunOptions


class BaseAppService(ABC):
    """Application facade shared by the HTTP and CLI transports."""

    @abstractmethod
    def health(self) -> AppResponse:
        raise NotImplementedError

    @abstractmethod
    def ingest_once(self) -> AppResponse:
        raise NotImplementedError

    async def runtime_health(self) -> AppResponse:
        """Return live runtime health where the selected service supports it."""

        return self.health()

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

    async def retrieve(
        self,
        query: str,
        *,
        tenant_id: str,
        top_k: int = 10,
        include_content: bool = False,
    ) -> AppResponse:
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

    @abstractmethod
    async def list_projects(self) -> AppResponse:
        """All projects (ML1 read side); data={"projects": list[Project]}."""
        raise NotImplementedError

    @abstractmethod
    async def get_project(self, project_id: str) -> AppResponse:
        """One project by id; raises HarborNotFoundError when missing."""
        raise NotImplementedError

    @abstractmethod
    async def list_sources(self, project_id: str | None = None) -> AppResponse:
        """Sources, optionally scoped to a project; data={"sources": [...]}."""
        raise NotImplementedError

    @abstractmethod
    async def get_source(self, source_id: str) -> AppResponse:
        """One source by id; raises HarborNotFoundError when missing."""
        raise NotImplementedError

    async def create_source(
        self,
        *,
        project_id: str,
        source_type: str,
        name: str,
        config: Mapping[str, object],
        schedule: str | None,
        actor: str,
    ) -> AppResponse:
        """Create a source (ML2 write side); data={"source": SourceConfig}."""
        raise NotImplementedError

    async def update_source(
        self,
        source_id: str,
        *,
        updates: dict[str, object],
        actor: str,
    ) -> AppResponse:
        """Update a source's mutable fields.

        ``updates`` carries only the keys the caller actually set (name,
        config, schedule, status) -- a key's absence means "leave alone",
        distinct from a present ``None``, e.g. clearing ``schedule`` back to
        manual-only sync. Callers build this from a partial-update payload
        via Pydantic's ``exclude_unset``, not by threading None-defaulted
        keyword args (schedule legitimately holds None).
        """
        raise NotImplementedError

    async def delete_source(self, source_id: str, *, actor: str) -> AppResponse:
        """Delete a source and forget its secrets."""
        raise NotImplementedError

    async def create_job(
        self,
        source_id: str,
        *,
        job_type: JobType = "bulk_ingest",
        dry_run: bool = False,
        options: JobRunOptions = JobRunOptions(),
        actor: str,
    ) -> AppResponse:
        """Create a Job for a source and start it via the Temporal ingestion path.

        data={"job": Job, "run": ..., "workflow": ..., ["result": ...]}.
        """
        raise NotImplementedError

    async def list_jobs(
        self,
        *,
        source_id: str | None = None,
        status: JobStatus | None = None,
    ) -> AppResponse:
        """Jobs filtered by source and/or status; data={"jobs": [Job, ...]}."""
        raise NotImplementedError

    async def get_job(self, job_id: str) -> AppResponse:
        """One job merged with its live Temporal state; data={"job", "live"}."""
        raise NotImplementedError

    async def get_job_result(self, job_id: str) -> AppResponse:
        """One job merged with its terminal Temporal result; data={"job", "result"}."""
        raise NotImplementedError

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
        raise NotImplementedError

    async def sync_job_progress(self) -> AppResponse:
        """Poll every running job's live Temporal state once; data={"synced": int}."""
        raise NotImplementedError

    async def stream_job_events(self, job_id: str) -> AsyncIterator[HarborEvent]:
        """Backlog replay then a live tail of a job's events; raises HarborNotFoundError."""
        raise NotImplementedError
        yield  # type: ignore[unreachable] # pragma: no cover - makes this an async generator

    @abstractmethod
    async def list_activity(self, limit: int = 50) -> AppResponse:
        """Most recent audit entries; data={"activity": [...]}."""
        raise NotImplementedError

    @abstractmethod
    async def get_settings(self) -> AppResponse:
        """The workspace settings document; data={"settings": WorkspaceSettings}."""
        raise NotImplementedError

    @abstractmethod
    async def get_metrics(self) -> AppResponse:
        """Dashboard summary counters; see workflow_control.metrics.summarize_metrics."""
        raise NotImplementedError
