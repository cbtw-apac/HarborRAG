"""Shared optional collaborators every model client accepts at construction.

Chat, embedding, and reranking clients take the same twelve runtime boundaries
and ownership flags. Declaring them once means a new boundary is added in one
place instead of three, and the defaults cannot drift between families.

Each family subclasses this to add its own provider registry and transport
boundary, which are the only genuinely family-specific dependencies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .budget import ModelBudgetPolicy
from .cache import ModelResponseCache
from .health import DeploymentHealthProbe
from .lifecycle import ResourceOwnership
from .redis_client import RedisConnectionLifecycle
from .routing_state import RoutingStateStore
from .runtime_services import ModelRuntimeServices
from .singleflight import SingleFlightCoordinator
from .telemetry import TelemetryDispatcher


class SharedModelDependencies(Protocol):
    """Read-only view of the boundaries every family resolves the same way.

    Chat declares its own dependency bundle because it carries a transport
    backend and shared connections, so the shared resolution helpers accept this
    structural view instead of requiring one common base class.
    """

    @property
    def cache(self) -> ModelResponseCache | None: ...
    @property
    def runtime_services(self) -> ModelRuntimeServices | None: ...
    @property
    def routing_state(self) -> RoutingStateStore | None: ...
    @property
    def singleflight(self) -> SingleFlightCoordinator | None: ...
    @property
    def budget(self) -> ModelBudgetPolicy | None: ...
    @property
    def redis(self) -> RedisConnectionLifecycle | None: ...
    @property
    def services_ownership(self) -> ResourceOwnership: ...
    @property
    def health_probe(self) -> DeploymentHealthProbe | None: ...
    @property
    def telemetry(self) -> TelemetryDispatcher | None: ...
    @property
    def telemetry_ownership(self) -> ResourceOwnership: ...
    @property
    def middleware(self) -> Sequence[object]: ...


@dataclass(frozen=True, slots=True)
class ModelClientDependencies:
    """Optional runtime boundaries and ownership shared by every model family.

    An ownership flag says whether the client closes a collaborator it was
    given: `OWNED` collaborators are closed with the client, `BORROWED` ones
    outlive it. Anything left unset is built by the client and therefore owned.

    Injected collaborators that several clients legitimately share -- telemetry
    and the runtime-services bundle -- default to `BORROWED` so closing one
    client never tears the shared instance out from under the others.
    """

    cache: ModelResponseCache | None = None
    runtime_services: ModelRuntimeServices | None = None
    routing_state: RoutingStateStore | None = None
    singleflight: SingleFlightCoordinator | None = None
    budget: ModelBudgetPolicy | None = None
    redis: RedisConnectionLifecycle | None = None
    services_ownership: ResourceOwnership = ResourceOwnership.BORROWED
    health_probe: DeploymentHealthProbe | None = None
    resource_ownership: ResourceOwnership = ResourceOwnership.OWNED
    telemetry: TelemetryDispatcher | None = None
    telemetry_ownership: ResourceOwnership = ResourceOwnership.BORROWED
    middleware: Sequence[object] = ()
