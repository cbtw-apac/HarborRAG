from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from harborrag_adapters.models.runtime.budget import ModelBudgetPolicy
from harborrag_adapters.models.runtime.cache import ModelResponseCache
from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle
from harborrag_adapters.models.runtime.health import DeploymentHealthProbe
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_adapters.models.runtime.redis_client import RedisConnectionLifecycle
from harborrag_adapters.models.runtime.routing_state import RoutingStateStore
from harborrag_adapters.models.runtime.runtime_services import ModelRuntimeServices
from harborrag_adapters.models.runtime.singleflight import SingleFlightCoordinator
from harborrag_adapters.models.runtime.telemetry import TelemetryDispatcher
from harborrag_core.models.chat import HarborChatResponse

from .backend import ChatBackend
from .registry import ProviderRegistry


class BatchFailureMode(StrEnum):
    """Control whether a chat batch stops or records independent item failures."""

    FAIL_FAST = "fail_fast"
    COLLECT = "collect"


@dataclass(frozen=True, slots=True)
class HarborChatBatchItem:
    """Store one ordered batch result or its item-specific exception."""

    index: int
    response: HarborChatResponse | None = None
    error: Exception | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether this item completed with a normalized response."""

        return self.response is not None and self.error is None


@dataclass(frozen=True, slots=True)
class HarborChatBatchResult:
    """Return ordered item outcomes for collect-mode batch execution."""

    items: tuple[HarborChatBatchItem, ...]

    @property
    def responses(self) -> tuple[HarborChatResponse, ...]:
        """Return successful responses in original request order."""

        return tuple(item.response for item in self.items if item.response is not None)

    @property
    def errors(self) -> tuple[Exception, ...]:
        """Return item failures in original request order."""

        return tuple(item.error for item in self.items if item.error is not None)


@dataclass(frozen=True, slots=True)
class ChatClientDependencies:
    """Optional adapter boundaries and ownership used to compose one chat client."""

    backend: ChatBackend | None = None
    cache: ModelResponseCache | None = None
    runtime_services: ModelRuntimeServices | None = None
    routing_state: RoutingStateStore | None = None
    singleflight: SingleFlightCoordinator | None = None
    budget: ModelBudgetPolicy | None = None
    redis: RedisConnectionLifecycle | None = None
    services_ownership: ResourceOwnership = ResourceOwnership.BORROWED
    health_probe: DeploymentHealthProbe | None = None
    connections: SharedConnectionLifecycle | None = None
    connection_ownership: ResourceOwnership = ResourceOwnership.BORROWED
    resource_ownership: ResourceOwnership = ResourceOwnership.OWNED
    telemetry: TelemetryDispatcher | None = None
    telemetry_ownership: ResourceOwnership = ResourceOwnership.BORROWED
    provider_registry: ProviderRegistry | None = None
    middleware: Sequence[object] = ()
