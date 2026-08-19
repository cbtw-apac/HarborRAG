"""Lazily created, singly closed runtime resources for the application service.

Resource lifecycle is a separate reason to change from the use cases that consume the
resources, so it lives here rather than as private helpers on AppService.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.ingestion.maintenance.projection_admin import (
    ProjectionAdministrationService,
)
from harborrag_runtime.sdk import HarborRAG
from harborrag_runtime.temporal.client import IngestionTemporalClient

if TYPE_CHECKING:
    from harborrag_core.ports.events import EventBusPort

    from ..ingestion.ports import PublicTaskStore
    from .factories import AppServiceFactories, TaskRegistry


class AppResources:
    """Create each runtime resource at most once and release them together."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        runtime_config: TemporalRuntimeConfig,
        factories: AppServiceFactories,
    ) -> None:
        self._settings = settings
        self._runtime_config = runtime_config
        self._factories = factories
        self._client: IngestionTemporalClient | None = None
        self._retrieval_runtime: HarborRAG | None = None
        self._task_registry: TaskRegistry | None = None
        self._projection_admin: ProjectionAdministrationService | None = None
        self._event_bus: EventBusPort | None = None
        self._client_lock = asyncio.Lock()
        self._task_registry_lock = asyncio.Lock()

    async def runtime_client(self) -> IngestionTemporalClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = await self._factories.client(self._runtime_config)
        return self._client

    def runtime_sdk(self) -> HarborRAG:
        if self._retrieval_runtime is None:
            self._retrieval_runtime = self._factories.retrieval_runtime(self._settings)
        return self._retrieval_runtime

    async def task_registry(self) -> TaskRegistry:
        if self._task_registry is not None:
            return self._task_registry
        async with self._task_registry_lock:
            if self._task_registry is None:
                self._task_registry = await self._factories.task_registry(self._settings)
        return self._task_registry

    async def public_task_store(self) -> PublicTaskStore:
        return await self.task_registry()

    def projection_administration(self) -> ProjectionAdministrationService:
        if self._projection_admin is None:
            self._projection_admin = self._factories.projection_admin(self._settings)
        return self._projection_admin

    def event_bus(self) -> EventBusPort:
        if self._event_bus is None:
            self._event_bus = self._factories.event_bus()
        return self._event_bus

    async def aclose(self) -> None:
        """Close every resource that was created, then report any failures together."""

        closers = [
            resource
            for resource in (
                self._retrieval_runtime.aclose if self._retrieval_runtime else None,
                self._task_registry.close if self._task_registry else None,
                self._projection_admin.close if self._projection_admin else None,
            )
            if resource is not None
        ]
        # return_exceptions keeps one failing resource from cancelling the siblings, so
        # every resource still gets its chance to release before we report.
        results = await asyncio.gather(
            *(close() for close in closers),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("application resource close failed", errors)


__all__ = ["AppResources"]
