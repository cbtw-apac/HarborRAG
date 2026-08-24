"""Typed collaborator factories for application-service composition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from harborrag_core.ports.events import EventBusPort
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.events import InProcessEventBus
from harborrag_runtime.ingestion.maintenance.projection_admin import (
    ProjectionAdministrationService,
)
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig
from harborrag_runtime.temporal.client import IngestionTemporalClient
from harborrag_runtime.temporal.schemas import SourceIngestionInput
from harborrag_runtime.temporal.submission import SourceSubmission, build_source_input
from harborrag_runtime.temporal.task_registry import IngestionTaskRegistry

from ..ingestion.ports import PublicTaskStore

type ClientFactory = Callable[
    [TemporalRuntimeConfig],
    Awaitable[IngestionTemporalClient],
]
type RetrievalRuntimeFactory = Callable[[RuntimeSettings], HarborRAG]
type SourceInputBuilder = Callable[
    [RuntimeSettings, SourceSubmission],
    SourceIngestionInput,
]
type ProjectionAdminFactory = Callable[
    [RuntimeSettings],
    ProjectionAdministrationService,
]
type EventBusFactory = Callable[[], EventBusPort]


class TaskRegistry(PublicTaskStore, Protocol):
    async def close(self) -> None: ...


type TaskRegistryFactory = Callable[[RuntimeSettings], Awaitable[TaskRegistry]]


def _retrieval_runtime(settings: RuntimeSettings) -> HarborRAG:
    return HarborRAG(HarborRAGConfig(runtime=settings))


@dataclass(frozen=True, slots=True)
class AppServiceFactories:
    """Collaborator factories, grouped so composition stays overridable in tests."""

    client: ClientFactory = IngestionTemporalClient.connect
    retrieval_runtime: RetrievalRuntimeFactory = _retrieval_runtime
    source_input_builder: SourceInputBuilder = build_source_input
    task_registry: TaskRegistryFactory = IngestionTaskRegistry.connect
    projection_admin: ProjectionAdminFactory = ProjectionAdministrationService
    event_bus: EventBusFactory = InProcessEventBus
