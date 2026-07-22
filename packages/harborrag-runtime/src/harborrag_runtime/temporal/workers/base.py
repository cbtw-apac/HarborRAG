"""Worker registration, bounded capacity, lifecycle, and health."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal.activities import (
    ChunkingActivities,
    DiscoveryActivities,
    IndexingActivities,
    MaintenanceActivities,
    ProcessingActivities,
)
from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.workflows import (
    ArtifactIngestionWorkflow,
    IngestionPartitionWorkflow,
    IngestionRunWorkflow,
)


class WorkerGroup(StrEnum):
    DISCOVERY = "discovery"
    PROCESSING = "processing"
    INDEXING = "indexing"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True, slots=True)
class WorkerRegistration:
    task_queue: str
    workflows: tuple[type, ...] = ()
    activities: tuple[Callable[..., Any], ...] = ()

    @property
    def activity_names(self) -> tuple[str, ...]:
        names = []
        for activity_method in self.activities:
            definition = getattr(activity_method, "__temporal_activity_definition", None)
            names.append(definition.name if definition is not None else "unknown")
        return tuple(names)


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    group: WorkerGroup
    running: bool
    shutting_down: bool
    task_queues: tuple[str, ...]
    dependency_ready: bool


def worker_registrations(
    group: WorkerGroup,
    config: TemporalRuntimeConfig,
    dependencies: RuntimeDependencies,
) -> tuple[WorkerRegistration, ...]:
    """Build inspectable queue registrations without starting any worker."""

    queues = config.task_queues
    discovery = DiscoveryActivities(dependencies)
    processing = ProcessingActivities(dependencies)
    chunking = ChunkingActivities(dependencies)
    indexing = IndexingActivities(dependencies)
    maintenance = MaintenanceActivities(dependencies)
    if group is WorkerGroup.DISCOVERY:
        return (
            WorkerRegistration(
                task_queue=queues.discovery,
                workflows=(IngestionRunWorkflow,),
                activities=(
                    discovery.discover_artifacts,
                    processing.preflight_artifact,
                ),
            ),
        )
    if group is WorkerGroup.PROCESSING:
        return (
            WorkerRegistration(
                task_queue=queues.chunking,
                workflows=(IngestionPartitionWorkflow, ArtifactIngestionWorkflow),
                activities=(chunking.chunk_artifact,),
            ),
            WorkerRegistration(
                task_queue=queues.connectors,
                activities=(processing.fetch_artifact,),
            ),
            WorkerRegistration(
                task_queue=queues.parsers,
                activities=(processing.parse_artifact,),
            ),
            WorkerRegistration(
                task_queue=queues.ocr,
                activities=(processing.parse_artifact,),
            ),
        )
    if group is WorkerGroup.INDEXING:
        return (
            WorkerRegistration(
                task_queue=queues.vector_index,
                activities=(indexing.index_artifact,),
            ),
            WorkerRegistration(
                task_queue=queues.graph_index,
                activities=(indexing.validate_artifact,),
            ),
        )
    return (
        WorkerRegistration(
            task_queue=queues.maintenance,
            activities=(
                maintenance.finalize_artifact,
                maintenance.reconcile_ingestion,
                maintenance.apply_resolution,
            ),
        ),
    )


class RuntimeWorkerGroup:
    """Own all Temporal workers and shared dependencies for one workload group."""

    def __init__(
        self,
        *,
        client: Client,
        config: TemporalRuntimeConfig,
        dependencies: RuntimeDependencies,
        group: WorkerGroup,
        manage_dependencies: bool = True,
    ) -> None:
        self._config = config
        self._dependencies = dependencies
        self._group = group
        self._manage_dependencies = manage_dependencies
        self._registrations = worker_registrations(group, config, dependencies)
        worker_config = config.worker
        self._workers = tuple(
            Worker(
                client,
                task_queue=registration.task_queue,
                workflows=registration.workflows,
                activities=registration.activities,
                identity=f"{worker_config.identity}-{group.value}",
                max_concurrent_activities=worker_config.max_concurrent_activities,
                max_concurrent_workflow_tasks=(worker_config.max_concurrent_workflow_tasks),
                max_concurrent_activity_task_polls=(worker_config.max_concurrent_activity_polls),
                max_concurrent_workflow_task_polls=(worker_config.max_concurrent_workflow_polls),
                graceful_shutdown_timeout=timedelta(
                    seconds=worker_config.graceful_shutdown_seconds
                ),
            )
            for registration in self._registrations
        )
        self._running = False
        self._shutting_down = False

    @property
    def registrations(self) -> tuple[WorkerRegistration, ...]:
        return self._registrations

    async def run(self) -> None:
        if self._manage_dependencies:
            await self._dependencies.start()
        self._running = True
        try:
            await asyncio.gather(*(worker.run() for worker in self._workers))
        finally:
            self._running = False
            if self._manage_dependencies:
                await self._dependencies.close()

    async def shutdown(self) -> None:
        self._shutting_down = True
        try:
            await asyncio.gather(*(worker.shutdown() for worker in self._workers))
        finally:
            self._running = False
            if self._manage_dependencies:
                await self._dependencies.close()

    async def health(self) -> WorkerHealth:
        dependency_health = await self._dependencies.health()
        return WorkerHealth(
            group=self._group,
            running=self._running,
            shutting_down=self._shutting_down,
            task_queues=tuple(item.task_queue for item in self._registrations),
            dependency_ready=bool(dependency_health["ready"]),
        )
