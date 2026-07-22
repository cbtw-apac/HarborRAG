"""Cohesive ownership of Temporal connectivity and runtime dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal.client import TemporalRuntimeClient
from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.workers import RuntimeWorkerGroup, WorkerGroup


@dataclass(slots=True)
class RuntimeLifecycle:
    """Create, share, health-check, and close one runtime service graph."""

    config: TemporalRuntimeConfig
    dependencies: RuntimeDependencies
    client: TemporalRuntimeClient
    _closed: bool = False

    @classmethod
    async def open(
        cls,
        config: TemporalRuntimeConfig,
        dependencies: RuntimeDependencies,
    ) -> RuntimeLifecycle:
        await dependencies.start()
        try:
            client = await TemporalRuntimeClient.connect(config)
        except Exception:
            await dependencies.close()
            raise
        return cls(config=config, dependencies=dependencies, client=client)

    def worker_group(self, group: WorkerGroup) -> RuntimeWorkerGroup:
        if self._closed:
            raise RuntimeError("runtime lifecycle is closed")
        return RuntimeWorkerGroup(
            client=self.client._client,
            config=self.config,
            dependencies=self.dependencies,
            group=group,
            manage_dependencies=False,
        )

    async def health(self) -> dict[str, object]:
        return {
            "closed": self._closed,
            "temporal_target": self.config.connection.target,
            "temporal_namespace": self.config.connection.namespace,
            "dependencies": await self.dependencies.health(),
        }

    async def close(self) -> None:
        if not self._closed:
            await self.dependencies.close()
            self._closed = True

    async def __aenter__(self) -> RuntimeLifecycle:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
