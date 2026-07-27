"""Production BaseAppService over a composed runtime (ST8).

M0 surface is health() + ingest_once() (the ABC's current contract); the
per-resource use-case methods grow with the M1+ routes.
"""

from __future__ import annotations

from harborrag_core.contracts.errors import HarborNotFoundError, HarborUnavailableError
from harborrag_runtime.composition import CompositionRoot, ControlPlaneRepositories

from harborrag_app.services.base import AppResponse, BaseAppService
from harborrag_app.services.metrics import summarize_metrics


class AppService(BaseAppService):
    """App-facing use-case facade bound to one composition root."""

    def __init__(self, composition: CompositionRoot) -> None:
        """Bind the service to an already-built composition."""
        self._composition = composition

    def _control_plane(self) -> ControlPlaneRepositories:
        control_plane = self._composition.control_plane
        if control_plane is None:
            raise HarborUnavailableError("control-plane database is not configured")
        return control_plane

    def health(self) -> AppResponse:
        """Runtime + engine diagnostics; ok=False when the runtime isn't ready."""
        diagnostics = self._composition.diagnostics()
        runtime = diagnostics.get("runtime")
        ready = bool(runtime.get("ready")) if isinstance(runtime, dict) else False
        return AppResponse(
            ok=ready,
            data={"diagnostics": diagnostics},
            error=None if ready else "runtime not ready",
        )

    def ingest_once(self) -> AppResponse:
        """Run the deterministic mock ingestion (real submission lands in M2)."""
        return AppResponse(True, self._composition.run_mock_ingestion())

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
        jobs = await control_plane.jobs.list()
        return AppResponse(True, summarize_metrics(projects, sources, jobs))
