"""Control-plane read use cases (ML1/M1): projects, sources, activity, settings, metrics.

Split out of client.py to keep that file under the repo's file-length gate;
mixed into AppService, which supplies the concrete _control_plane().
"""

from __future__ import annotations

from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_runtime.composition import ControlPlaneRepositories

from .metrics import summarize_metrics
from .schemas import AppResponse


class ControlPlaneReadsMixin:
    """Read-side control-plane use cases shared by AppService."""

    def _control_plane(self) -> ControlPlaneRepositories:
        raise NotImplementedError

    async def list_projects(self) -> AppResponse:
        """All projects from the control-plane DB (ML1 read side)."""
        projects = await self._control_plane().projects.list()
        return AppResponse(True, {"projects": projects})

    async def get_project(self, project_id: str) -> AppResponse:
        """One project by id; raises HarborNotFoundError when missing."""
        project = await self._control_plane().projects.get(project_id)
        if project is None:
            raise HarborNotFoundError(f"project {project_id!r} not found")
        return AppResponse(True, {"project": project})

    async def list_sources(self, project_id: str | None = None) -> AppResponse:
        """Sources from the control-plane DB, optionally scoped to a project."""
        sources = await self._control_plane().sources.list(project_id)
        return AppResponse(True, {"sources": sources})

    async def get_source(self, source_id: str) -> AppResponse:
        """One source by id; raises HarborNotFoundError when missing."""
        source = await self._control_plane().sources.get(source_id)
        if source is None:
            raise HarborNotFoundError(f"source {source_id!r} not found")
        return AppResponse(True, {"source": source})

    async def list_activity(self, limit: int = 50) -> AppResponse:
        """Most recent audit entries from the control-plane DB."""
        activity = await self._control_plane().activity.list(limit)
        return AppResponse(True, {"activity": activity})

    async def get_settings(self) -> AppResponse:
        """The workspace settings document (empty document if never written)."""
        settings = await self._control_plane().settings.get()
        return AppResponse(True, {"settings": settings})

    async def get_metrics(self) -> AppResponse:
        """Dashboard summary counters aggregated from the control-plane DB."""
        control_plane = self._control_plane()
        projects = await control_plane.projects.list()
        sources = await control_plane.sources.list()
        jobs_by_status = await control_plane.jobs.count_by_status()
        return AppResponse(True, summarize_metrics(projects, sources, jobs_by_status))
