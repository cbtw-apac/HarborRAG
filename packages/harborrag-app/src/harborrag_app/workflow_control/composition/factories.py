"""Typed collaborator factories for application-service composition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from harborrag_core.ports.events import EventBusPort
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.events import InProcessEventBus
from harborrag_runtime.ingestion.maintenance.projection_admin import (
    ProjectionAdministrationService,
)
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig
from harborrag_runtime.temporal.schemas import SourceIngestionInput
from harborrag_runtime.temporal.submission import SourceSubmission, build_source_input
from harborrag_runtime.temporal.task_registry import IngestionTaskRegistry

from ..errors import MissingOptionalDependencyError
from ..ingestion.ports import PublicTaskStore

if TYPE_CHECKING:
    from harborrag_runtime.temporal.client import IngestionTemporalClient

TEMPORAL_EXTRA_HINT = (
    'Durable ingestion commands need the Temporal client: pip install "harborrag[temporal]"'
)

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


async def connect_temporal_client(config: TemporalRuntimeConfig) -> IngestionTemporalClient:
    """Connect the Temporal client, importing it only now.

    ``temporalio`` ships with the ``temporal`` extra, not with a bare or ``[local]`` install.
    Direct-mode commands never call this, so they must not pay for the import either.
    """

    try:
        from harborrag_runtime.temporal.client import IngestionTemporalClient
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".")[0] == "temporalio":
            raise MissingOptionalDependencyError(TEMPORAL_EXTRA_HINT) from exc
        raise
    return await IngestionTemporalClient.connect(config)


def _retrieval_runtime(settings: RuntimeSettings) -> HarborRAG:
    return HarborRAG(HarborRAGConfig(runtime=settings))


@dataclass(frozen=True, slots=True)
class AppServiceFactories:
    """Collaborator factories, grouped so composition stays overridable in tests."""

    client: ClientFactory = connect_temporal_client
    retrieval_runtime: RetrievalRuntimeFactory = _retrieval_runtime
    source_input_builder: SourceInputBuilder = build_source_input
    task_registry: TaskRegistryFactory = IngestionTaskRegistry.connect
    projection_admin: ProjectionAdminFactory = ProjectionAdministrationService
    event_bus: EventBusFactory = InProcessEventBus
