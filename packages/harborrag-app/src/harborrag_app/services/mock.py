from __future__ import annotations

from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.project import Project
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.testing.fakes import (
    FakeActivityRepository,
    FakeJobRepository,
    FakeProjectRepository,
    FakeSettingsRepository,
    FakeSourceRepository,
)

from harborrag_app.services.base import AppResponse, BaseAppService
from harborrag_app.services.metrics import summarize_metrics


class MockAppService(BaseAppService):
    """Dev-mode service: no control-plane DB, so reads are dict-backed.

    Fresh, empty fake repositories per instance are honest about mock mode
    having no persisted data — they exercise the same ports production code
    depends on, just with nothing in them yet. Tests may pass seed data to
    exercise the read routes without a real database.
    """

    def __init__(
        self,
        projects: list[Project] | None = None,
        sources: list[SourceConfig] | None = None,
        activity: list[ActivityEntry] | None = None,
        settings: WorkspaceSettings | None = None,
    ) -> None:
        self._projects = FakeProjectRepository(
            {project.id: project for project in projects or []}
        )
        self._sources = FakeSourceRepository(
            {source.id: source for source in sources or []}
        )
        self._activity = FakeActivityRepository(list(activity or []))
        self._settings = FakeSettingsRepository(settings or WorkspaceSettings())
        self._jobs = FakeJobRepository()

    def health(self) -> AppResponse:
        from harborrag_runtime.composition import CompositionRoot

        return AppResponse(True, {"diagnostics": CompositionRoot.local().diagnostics()})

    def ingest_once(self) -> AppResponse:
        from harborrag_runtime.composition import CompositionRoot

        return AppResponse(True, CompositionRoot.local().run_mock_ingestion())

    async def list_projects(self) -> AppResponse:
        return AppResponse(True, {"projects": await self._projects.list()})

    async def get_project(self, project_id: str) -> AppResponse:
        project = await self._projects.get(project_id)
        if project is None:
            return AppResponse(False, error="not_found")
        return AppResponse(True, {"project": project})

    async def list_sources(self, project_id: str | None = None) -> AppResponse:
        return AppResponse(True, {"sources": await self._sources.list(project_id)})

    async def get_source(self, source_id: str) -> AppResponse:
        source = await self._sources.get(source_id)
        if source is None:
            return AppResponse(False, error="not_found")
        return AppResponse(True, {"source": source})

    async def list_activity(self, limit: int = 50) -> AppResponse:
        return AppResponse(True, {"activity": await self._activity.list(limit)})

    async def get_settings(self) -> AppResponse:
        return AppResponse(True, {"settings": await self._settings.get()})

    async def get_metrics(self) -> AppResponse:
        projects = await self._projects.list()
        sources = await self._sources.list()
        jobs = await self._jobs.list()
        return AppResponse(True, summarize_metrics(projects, sources, jobs))
