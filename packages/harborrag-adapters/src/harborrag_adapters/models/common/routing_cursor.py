from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from harborrag_core.models.errors import HarborModelError
from pydantic import BaseModel

from .config import RetryPolicyConfig
from .model_config import logical_fallback_chain
from .routing import DeploymentLike, DeploymentRuntime, DeploymentSelector, NoHealthyDeploymentError
from .routing_types import (
    RoutedAttempt,
    RoutedResult,
    RoutingTransition,
    RoutingTransitionType,
)


@dataclass(frozen=True, slots=True)
class SelectedRoutingAttempt[D: DeploymentLike]:
    """Carry internal deployment runtime state for one selected attempt."""

    logical_model: str
    state: DeploymentRuntime[D]
    attempt: int

    @property
    def public(self) -> RoutedAttempt[D]:
        """Return the provider-neutral public attempt value."""

        return RoutedAttempt(self.logical_model, self.state.config, self.attempt)


class RoutingExecutionCursor[D: DeploymentLike]:
    """Advance retry, deployment-failover, and model-fallback state."""

    def __init__(self, models: Mapping[str, Any], start: str, policy: RetryPolicyConfig) -> None:
        """Initialize routing counters and the bounded logical fallback chain."""

        self.models = models
        self.policy = policy
        self.chain = logical_fallback_chain(models, start)[: policy.max_model_fallbacks + 1]
        self.logical_index = 0
        self.excluded: set[str] = set()
        self.current: DeploymentRuntime[D] | None = None
        self.attempt = 0
        self.retry_count = 0
        self.deployment_failover_count = 0
        self.model_fallback_count = 0

    @property
    def counts(self) -> tuple[int, int, int]:
        """Return retry, deployment-failover, and model-fallback counters."""

        return self.retry_count, self.deployment_failover_count, self.model_fallback_count

    @property
    def diagnostics(self) -> dict[str, int]:
        """Return sanitized transition counters for errors and response metadata."""

        return {
            "same_deployment_retries": self.retry_count,
            "deployment_failovers": self.deployment_failover_count,
            "model_fallbacks": self.model_fallback_count,
        }

    def next_attempt_sync(
        self, selector: DeploymentSelector[D]
    ) -> SelectedRoutingAttempt[D] | None:
        """Select the next available deployment for synchronous execution."""

        while self.logical_index < len(self.chain):
            logical = self.chain[self.logical_index]
            if self.current is None:
                try:
                    self.current = selector.select_sync(
                        logical, self.models[logical].deployments, exclude=self.excluded
                    )
                except NoHealthyDeploymentError:
                    if not self._advance_model():
                        return None
                    continue
            return SelectedRoutingAttempt(logical, self.current, self.attempt + 1)
        return None

    async def next_attempt(
        self, selector: DeploymentSelector[D]
    ) -> SelectedRoutingAttempt[D] | None:
        """Select the next available deployment for asynchronous execution."""

        return self.next_attempt_sync(selector)

    def failed(self, *, retryable: bool, current_available: bool = True) -> bool:
        """Record a failure and report whether policy permits another attempt."""

        if not retryable:
            return False
        self.attempt += 1
        if current_available and self.attempt < self.policy.same_deployment_attempts:
            self.retry_count += 1
            return True
        if self.current is not None:
            self.excluded.add(self.current.config.name)
        self.current = None
        self.attempt = 0
        logical = self.chain[self.logical_index]
        remaining = any(
            deployment.enabled and deployment.name not in self.excluded
            for deployment in self.models[logical].deployments
        )
        if remaining and self.deployment_failover_count < self.policy.max_deployment_failovers:
            self.deployment_failover_count += 1
            return True
        return self._advance_model()

    def transition(
        self,
        before: tuple[int, int, int],
        attempt: RoutedAttempt[D],
        error: HarborModelError,
    ) -> RoutingTransition[D]:
        """Describe which routing transition occurred after an attempt failed."""

        kind = (
            RoutingTransitionType.MODEL_FALLBACK
            if self.model_fallback_count > before[2]
            else RoutingTransitionType.DEPLOYMENT_FALLBACK
            if self.deployment_failover_count > before[1]
            else RoutingTransitionType.RETRY
        )
        return RoutingTransition(
            kind,
            attempt,
            error,
            self.retry_count,
            self.deployment_failover_count,
            self.model_fallback_count,
        )

    def result[T: BaseModel](self, value: T, attempt: RoutedAttempt[D]) -> RoutedResult[T, D]:
        """Attach routing counters to a successful normalized response."""

        metadata = dict(getattr(value, "provider_metadata", {}))
        metadata["routing"] = self.diagnostics
        routed = value.model_copy(
            update={
                "retry_count": self.retry_count,
                "fallback_count": self.deployment_failover_count + self.model_fallback_count,
                "provider_metadata": metadata,
            }
        )
        return RoutedResult(
            routed,
            attempt,
            self.retry_count,
            self.deployment_failover_count,
            self.model_fallback_count,
        )

    def _advance_model(self) -> bool:
        if self.logical_index + 1 >= len(self.chain):
            return False
        self.logical_index += 1
        self.model_fallback_count += 1
        self.excluded.clear()
        self.current = None
        self.attempt = 0
        return True
