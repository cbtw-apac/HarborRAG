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
    """Application service facade for HTTP and CLI."""

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
