from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AppResponse:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class BaseAppService(ABC):
    """Application service facade for HTTP and CLI.

    TODO: Implement production service methods with request context, auth/permission checks,
    input validation, structured error envelopes, and fail-closed defaults.
    """

    @abstractmethod
    def health(self) -> AppResponse:
        raise NotImplementedError

    @abstractmethod
    def ingest_once(self) -> AppResponse:
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
        """Dashboard summary counters; see services.metrics.summarize_metrics."""
        raise NotImplementedError
