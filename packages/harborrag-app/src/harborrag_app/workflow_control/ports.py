from __future__ import annotations

from abc import ABC, abstractmethod

from .schemas import AppResponse


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
