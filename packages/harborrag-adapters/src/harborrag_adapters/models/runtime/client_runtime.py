from __future__ import annotations

from typing import Any, cast

from .health import ActiveHealthMonitor, HealthCheckResult
from .introspection import ClientDescription, DeploymentHealthView, RouteExplanation


class ModelClientRuntimeMixin:
    """Expose sanitized runtime inspection and optional active health operations."""

    _introspector: Any
    _health_monitor: ActiveHealthMonitor | None

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
