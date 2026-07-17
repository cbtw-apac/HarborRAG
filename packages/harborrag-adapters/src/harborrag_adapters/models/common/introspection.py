from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .config import ModelClientConfig, RoutingStrategy
from .routing import DeploymentSelector
from .routing_state import RoutingStateSnapshot


class DeploymentHealthView(BaseModel):
    """Expose sanitized configured and live state for one model deployment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_model: str
    deployment: str
    provider: str
    provider_model: str
    enabled: bool
    available: bool
    active_requests: int = Field(ge=0)
    consecutive_failures: int = Field(ge=0)
    circuit_open_until: float = Field(ge=0)
    last_latency_ms: float | None = Field(default=None, ge=0)
    active_healthy: bool | None = None
    active_checked_at: float | None = Field(default=None, ge=0)


class RouteExplanation(BaseModel):
    """Explain eligible deployments and one deterministic preview selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_model: str
    strategy: str
    eligible_deployments: tuple[str, ...]
    excluded_deployments: dict[str, str]
    preview_selection: str | None = None


class ClientDescription(BaseModel):
    """Describe one client without exposing credentials, prompts, or response data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: str
    backend: str
    default_model: str
    logical_models: tuple[str, ...]
    aliases: dict[str, str]
    routing_engine: str
    routing_strategy: str
    cache_backend: str
    distributed_routing: bool
    singleflight_backend: str
    budget_enabled: bool


class ModelRuntimeIntrospector:
    """Provide stable runtime inspection without exposing mutable routing internals."""

    def __init__(
        self,
        config: ModelClientConfig,
        models: Mapping[str, Any],
        selector: DeploymentSelector[Any],
        *,
        family: str,
        backend: str,
    ) -> None:
        """Bind sanitized configuration and shared routing state."""

        self._config = config
        self._models = models
        self._selector = selector
        self._family = family
        self._backend = backend

    def describe(self) -> ClientDescription:
        """Return a sanitized client and deployment-mode description."""

        aliases = {
            alias: logical
            for logical, model in self._models.items()
            for alias in getattr(model, "aliases", ())
        }
        return ClientDescription(
            family=self._family,
            backend=self._backend,
            default_model=self._config.default_model,
            logical_models=tuple(sorted(self._models)),
            aliases=aliases,
            routing_engine=self._config.routing.engine.value,
            routing_strategy=self._config.routing.strategy.value,
            cache_backend=self._config.cache.backend.value,
            distributed_routing=self._config.routing.state_backend.value == "redis",
            singleflight_backend=self._config.singleflight.backend.value,
            budget_enabled=self._config.budget.enabled,
        )

    def list_models(self) -> tuple[str, ...]:
        """Return canonical logical model names in stable order."""

        return tuple(sorted(self._models))

    def health(self) -> tuple[DeploymentHealthView, ...]:
        """Return current distributed health and admission state."""

        return self._health_views(self._selector.snapshots())

    async def ahealth(self) -> tuple[DeploymentHealthView, ...]:
        """Return current distributed health asynchronously."""

        return self._health_views(await self._selector.asnapshots())

    def explain_route(self, logical_model: str) -> RouteExplanation:
        """Explain which deployments are currently eligible without changing selector state."""

        if logical_model not in self._models:
            raise KeyError(f"unknown logical model: {logical_model}")
        views = [view for view in self.health() if view.logical_model == logical_model]
        eligible = tuple(view.deployment for view in views if view.available)
        excluded = {
            view.deployment: _exclusion_reason(view) for view in views if not view.available
        }
        return RouteExplanation(
            logical_model=logical_model,
            strategy=self._config.routing.strategy.value,
            eligible_deployments=eligible,
            excluded_deployments=excluded,
            preview_selection=self._preview(logical_model, eligible),
        )

    def _health_views(
        self, snapshots: Mapping[str, RoutingStateSnapshot]
    ) -> tuple[DeploymentHealthView, ...]:
        now = time.time()
        result: list[DeploymentHealthView] = []
        for logical, model in sorted(self._models.items()):
            for deployment in sorted(model.deployments, key=lambda item: (item.order, item.name)):
                key = f"{logical}:{deployment.name}"
                snapshot = snapshots.get(key, RoutingStateSnapshot())
                available = deployment.enabled and snapshot.available(
                    now,
                    health_stale_seconds=self._config.routing.active_health.stale_after_seconds,
                )
                provider = getattr(deployment.provider, "value", deployment.provider)
                result.append(
                    DeploymentHealthView(
                        logical_model=logical,
                        deployment=deployment.name,
                        provider=str(provider),
                        provider_model=deployment.model,
                        enabled=deployment.enabled,
                        available=available,
                        active_requests=snapshot.active_requests,
                        consecutive_failures=snapshot.consecutive_failures,
                        circuit_open_until=snapshot.circuit_open_until,
                        last_latency_ms=snapshot.last_latency_ms,
                        active_healthy=snapshot.active_healthy,
                        active_checked_at=snapshot.active_checked_at,
                    )
                )
        return tuple(result)

    def _preview(self, logical: str, eligible: tuple[str, ...]) -> str | None:
        if not eligible:
            return None
        deployments = {
            deployment.name: deployment for deployment in self._models[logical].deployments
        }
        priority = min(deployments[name].order for name in eligible)
        candidates = tuple(name for name in eligible if deployments[name].order == priority)
        strategy = self._config.routing.strategy
        if strategy in {RoutingStrategy.ORDERED, RoutingStrategy.ROUND_ROBIN}:
            return candidates[0]
        if strategy is RoutingStrategy.WEIGHTED:
            return max(candidates, key=lambda name: deployments[name].weight)
        views = {view.deployment: view for view in self.health() if view.logical_model == logical}
        if strategy is RoutingStrategy.LEAST_BUSY:
            return min(candidates, key=lambda name: (views[name].active_requests, name))
        return min(
            candidates,
            key=lambda name: (
                float("inf")
                if views[name].last_latency_ms is None
                else views[name].last_latency_ms,
                name,
            ),
        )


def _exclusion_reason(view: DeploymentHealthView) -> str:
    if not view.enabled:
        return "disabled"
    if view.active_healthy is False:
        return "active_health_failed"
    if view.circuit_open_until > time.time():
        return "circuit_open"
    return "unavailable"
