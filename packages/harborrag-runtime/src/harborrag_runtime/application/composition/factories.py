"""Typed collaborator factories for application-service composition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from harborrag_core.ingestion import IngestionTask, IngestionTaskState
from harborrag_core.ports.events import EventBusPort
from harborrag_core.security import AccessContext
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.events import InProcessEventBus
from harborrag_runtime.execution.gateway import (
    IngestionGatewayDescription,
    connect_ingestion_gateway,
    connect_schedule_backend,
    describe_ingestion_gateway,
    prepare_configured_source_submission,
)
from harborrag_runtime.execution.gateway import (
    connect_temporal_client as connect_temporal_client,
)
from harborrag_runtime.execution.task_registry import IngestionTaskRegistry
from harborrag_runtime.ingestion_contracts import IngestionGateway
from harborrag_runtime.scheduling import ScheduleBackend
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig
from harborrag_runtime.temporal.optional import load_temporal_attribute

from ..control_plane.schedule_models import ScheduleTimers
from ..ingestion.connection_sources import SourcePreparation
from ..ingestion.ports import PublicTaskStore, SourceInputBuilder

type ClientFactory = Callable[
    [RuntimeSettings],
    Awaitable[IngestionGateway],
]
type ScheduleBackendFactory = Callable[[RuntimeSettings], Awaitable[ScheduleBackend]]
type ScheduleTimersFactory = Callable[[RuntimeSettings], Awaitable[ScheduleTimers]]
type GatewayDescriptionFactory = Callable[[RuntimeSettings], IngestionGatewayDescription]
type RetrievalRuntimeFactory = Callable[[RuntimeSettings], HarborRAG]
type EventBusFactory = Callable[[], EventBusPort]


class TaskRegistry(PublicTaskStore, Protocol):
    """Task registry with the public task store plus the connection run queries."""

    async def latest_for_connection(
        self, *, tenant_id: str, connection_id: str
    ) -> IngestionTask | None:
        """Return the connection's most recently submitted run, or ``None`` when it has none."""

    async def active_for_connection(
        self, *, tenant_id: str, connection_id: str
    ) -> IngestionTask | None:
        """Return the connection's run that has not ended, or ``None`` when it has none."""

    async def connection_document_count(self, *, tenant_id: str, connection_id: str) -> int:
        """Count the documents the connection's runs have recorded."""

    async def source_keys(self, *, tenant_id: str) -> frozenset[str]:
        """Return every source key the tenant's runs were registered under."""

    async def source_scope_ids_by_connection(
        self, *, tenant_id: str, connection_ids: Sequence[str]
    ) -> dict[str, list[str]]:
        """Return the source scope IDs each of ``connection_ids`` was ingested under."""

    async def allowed_source_scope_ids(
        self, tenant_id: str, *, access: AccessContext, limit: int = 10000
    ) -> tuple[str, ...]:
        """Return every source scope ``access`` may read in ``tenant_id``."""

    async def published_connection_ids(
        self, *, tenant_id: str, connection_ids: Sequence[str]
    ) -> set[str]:
        """Return which of ``connection_ids`` have published documents."""

    async def fail_if_unsubmitted(
        self,
        task_id: str,
        *,
        summary: Mapping[str, object],
    ) -> IngestionTaskState | None:
        """Fail the run when its workflow start was never confirmed; return its state afterwards."""

    async def close(self) -> None:
        """Release the registry's database resources."""


type TaskRegistryFactory = Callable[[RuntimeSettings], Awaitable[TaskRegistry]]


def _retrieval_runtime(settings: RuntimeSettings) -> HarborRAG:
    return HarborRAG(HarborRAGConfig(runtime=settings))


async def _schedule_timers(settings: RuntimeSettings) -> ScheduleTimers:
    """Connect the Temporal timers of ``/v1/admin/schedules``, importing Temporal only now."""
    connect = cast(  # the loader returns object; the module defines this coroutine function
        "ScheduleTimersFactory",
        load_temporal_attribute(
            "harborrag_runtime.temporal.schedule_timers", "connect_schedule_timers"
        ),
    )
    return await connect(settings)


@dataclass(frozen=True, slots=True)
class AppServiceFactories:
    """Collaborator factories, grouped so composition stays overridable in tests."""

    client: ClientFactory = connect_ingestion_gateway
    schedule_backend: ScheduleBackendFactory = connect_schedule_backend
    schedule_timers: ScheduleTimersFactory = _schedule_timers
    ingestion_description: GatewayDescriptionFactory = describe_ingestion_gateway
    retrieval_runtime: RetrievalRuntimeFactory = _retrieval_runtime
    source_input_builder: SourceInputBuilder | None = None
    source_preparation: SourcePreparation = prepare_configured_source_submission
    task_registry: TaskRegistryFactory = IngestionTaskRegistry.connect
    event_bus: EventBusFactory = InProcessEventBus
