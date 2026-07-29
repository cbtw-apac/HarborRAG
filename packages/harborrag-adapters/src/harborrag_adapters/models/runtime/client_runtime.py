from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from .client_dependencies import SharedModelDependencies
from .config import ModelClientConfig
from .health import ActiveHealthMonitor, HealthCheckResult
from .introspection import ClientDescription, DeploymentHealthView, RouteExplanation
from .lifecycle import ResourceOwnership
from .middleware import MiddlewarePipeline
from .runtime_services import ModelRuntimeServices, build_runtime_services
from .telemetry import TelemetryDispatcher


class ModelClientRuntimeMixin:
    """Expose sanitized runtime inspection and optional active health operations.

    The `_resolve_*` helpers hold the dependency resolution and ownership rules
    that chat, embedding, and reranking all share, so a change to ownership
    semantics or health-monitor wiring happens once instead of in three
    constructors.
    """

    _introspector: Any
    _health_monitor: ActiveHealthMonitor | None
    _middleware: MiddlewarePipeline
    _telemetry: TelemetryDispatcher
    _owns_telemetry: bool
    _services: ModelRuntimeServices
    _owns_services: bool

    @staticmethod
    def _require_health_probe(
        config: ModelClientConfig,
        dependencies: SharedModelDependencies,
    ) -> None:
        """Reject automatic active health checking without an injected probe."""

        if config.routing.active_health.start_automatically and dependencies.health_probe is None:
            raise ValueError(
                "routing.active_health.start_automatically requires an injected health probe"
            )

    def _resolve_shared_runtime(
        self,
        config: ModelClientConfig,
        dependencies: SharedModelDependencies,
        *,
        family: str,
    ) -> None:
        """Bind middleware, telemetry, and runtime services with their ownership.

        An injected collaborator is closed with the client only when the caller
        marked it `OWNED`; anything built here is always owned.
        """

        self._middleware = MiddlewarePipeline(dependencies.middleware)
        self._telemetry = dependencies.telemetry or TelemetryDispatcher(
            (),
            config=config.observability,
        )
        self._owns_telemetry = (
            dependencies.telemetry is None
            or dependencies.telemetry_ownership is ResourceOwnership.OWNED
        )
        self._services = dependencies.runtime_services or build_runtime_services(
            config,
            family=family,
            cache=dependencies.cache,
            routing_state=dependencies.routing_state,
            singleflight=dependencies.singleflight,
            budget=dependencies.budget,
            redis=dependencies.redis,
        )
        self._owns_services = (
            dependencies.runtime_services is None
            or dependencies.services_ownership is ResourceOwnership.OWNED
        )

    def _resolve_health_monitor(
        self,
        config: ModelClientConfig,
        dependencies: SharedModelDependencies,
        *,
        models: Mapping[str, Any],
    ) -> None:
        """Attach the optional active health monitor and honor automatic start."""

        probe = dependencies.health_probe
        self._health_monitor = (
            ActiveHealthMonitor(
                models,
                config=config.routing.active_health,
                store=self._services.routing_state,
                probe=probe,
            )
            if probe is not None
            else None
        )
        if config.routing.active_health.start_automatically:
            self.start_health_monitor()

    def describe(self) -> ClientDescription:
        """Return a sanitized description of configuration and runtime backends."""

        return cast(ClientDescription, self._introspector.describe())

    def list_models(self) -> tuple[str, ...]:
        """Return configured canonical logical-model names in stable order."""

        return cast(tuple[str, ...], self._introspector.list_models())

    def deployment_health(self) -> tuple[DeploymentHealthView, ...]:
        """Return current passive and active deployment health snapshots."""

        return cast(tuple[DeploymentHealthView, ...], self._introspector.health())

    async def adeployment_health(self) -> tuple[DeploymentHealthView, ...]:
        """Return current deployment health through the asynchronous state store."""

        return cast(tuple[DeploymentHealthView, ...], await self._introspector.ahealth())

    def explain_route(self, logical_model: str) -> RouteExplanation:
        """Explain currently eligible deployments without mutating routing state."""

        return cast(RouteExplanation, self._introspector.explain_route(logical_model))

    def check_deployment_health(self) -> tuple[tuple[str, HealthCheckResult], ...]:
        """Run one configured active health-check cycle synchronously."""

        return self._require_health_monitor().check_once()

    async def acheck_deployment_health(
        self,
    ) -> tuple[tuple[str, HealthCheckResult], ...]:
        """Run one configured active health-check cycle asynchronously."""

        return await self._require_health_monitor().acheck_once()

    def start_health_monitor(self) -> None:
        """Start recurring active deployment health checks."""

        self._require_health_monitor().start()

    async def astart_health_monitor(self) -> None:
        """Start recurring active deployment health checks on the current event loop."""

        await self._require_health_monitor().astart()

    def _require_health_monitor(self) -> ActiveHealthMonitor:
        if self._health_monitor is None:
            raise RuntimeError("active health requires an injected deployment health probe")
        return self._health_monitor
