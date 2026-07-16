from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from harborrag_core.models.errors import HarborModelError
from pydantic import BaseModel

from .config import RetryPolicyConfig, RoutingConfig
from .model_config import logical_fallback_chain
from .retry import RetryController
from .routing import DeploymentLike, DeploymentRuntime, DeploymentSelector, NoHealthyDeploymentError


@dataclass(frozen=True, slots=True)
class RoutedAttempt[D: DeploymentLike]:
    """Identify one concrete attempt within the routing state machine."""

    logical_model: str
    deployment: D
    attempt: int


@dataclass(frozen=True, slots=True)
class RoutedResult[T: BaseModel, D: DeploymentLike]:
    """Return a normalized response with explicit transition counters."""

    value: T
    attempt: RoutedAttempt[D]
    retry_count: int
    deployment_failover_count: int
    model_fallback_count: int


class RoutingTransitionType(StrEnum):
    """Distinguish retry and fallback transitions for policy telemetry."""

    RETRY = "retry"
    DEPLOYMENT_FALLBACK = "deployment_fallback"
    MODEL_FALLBACK = "model_fallback"


@dataclass(frozen=True, slots=True)
class RoutingTransition[D: DeploymentLike]:
    """Describe one retry or fallback decision after a provider failure."""

    kind: RoutingTransitionType
    attempt: RoutedAttempt[D]
    error: HarborModelError
    retry_count: int
    deployment_failover_count: int
    model_fallback_count: int


type ErrorNormalizer[D: DeploymentLike] = Callable[[Exception, str, D], HarborModelError]
type SyncInvoke[D: DeploymentLike] = Callable[[RoutedAttempt[D]], Any]
type AsyncInvoke[D: DeploymentLike] = Callable[[RoutedAttempt[D]], Awaitable[Any]]
type ResponseNormalizer[T: BaseModel, D: DeploymentLike] = Callable[[Any, str, D, float], T]
type SyncTransitionCallback[D: DeploymentLike] = Callable[[RoutingTransition[D]], None]
type AsyncTransitionCallback[D: DeploymentLike] = Callable[[RoutingTransition[D]], Awaitable[None]]


class RoutedModelExecutor[D: DeploymentLike]:
    """Own retry and fallback transitions shared by every model family."""

    def __init__(
        self,
        models: Mapping[str, Any],
        *,
        routing: RoutingConfig,
        retry: RetryPolicyConfig,
    ) -> None:
        self._models = models
        self._retry = RetryController(retry)
        self._policy = retry
        self._selector = DeploymentSelector(
            {name: model.deployments for name, model in models.items()},
            strategy=routing.strategy,
            circuit_breaker=routing.circuit_breaker,
            enable_health_tracking=routing.enable_health_tracking,
        )

    def execute[T: BaseModel](
        self,
        logical_model: str,
        *,
        invoke: SyncInvoke[D],
        normalize: ResponseNormalizer[T, D],
        normalize_error: ErrorNormalizer[D],
        on_transition: SyncTransitionCallback[D] | None = None,
    ) -> RoutedResult[T, D]:
        cursor: RoutingExecutionCursor[D] = RoutingExecutionCursor(
            self._models, logical_model, self._policy
        )
        while attempt := cursor.next_attempt_sync(self._selector):
            started = time.perf_counter()
            state = attempt.state
            try:
                with self._selector.lease_sync(state):
                    raw = invoke(attempt.public)
                latency = (time.perf_counter() - started) * 1_000
                self._selector.record_success_sync(state, latency)
                value = normalize(raw, attempt.logical_model, state.config, latency)
                return cursor.result(value, attempt.public)
            except Exception as exc:
                error = normalize_error(exc, attempt.logical_model, state.config)
                retryable = bool(error.retryable)
                self._selector.record_failure_sync(state, retryable=retryable)
                before = cursor.counts
                if not cursor.failed(retryable=retryable):
                    raise error from exc
                if on_transition is not None:
                    on_transition(cursor.transition(before, attempt.public, error))
                delay = self._retry.delay_seconds(cursor.retry_count)
                if delay:
                    time.sleep(delay)
        raise RuntimeError("routing terminated without a response")

    async def aexecute[T: BaseModel](
        self,
        logical_model: str,
        *,
        invoke: AsyncInvoke[D],
        normalize: ResponseNormalizer[T, D],
        normalize_error: ErrorNormalizer[D],
        on_transition: AsyncTransitionCallback[D] | None = None,
    ) -> RoutedResult[T, D]:
        cursor: RoutingExecutionCursor[D] = RoutingExecutionCursor(
            self._models, logical_model, self._policy
        )
        while attempt := await cursor.next_attempt(self._selector):
            started = time.perf_counter()
            state = attempt.state
            try:
                async with self._selector.lease(state):
                    raw = await invoke(attempt.public)
                latency = (time.perf_counter() - started) * 1_000
                await self._selector.record_success(state, latency)
                value = normalize(raw, attempt.logical_model, state.config, latency)
                return cursor.result(value, attempt.public)
            except Exception as exc:
                error = normalize_error(exc, attempt.logical_model, state.config)
                retryable = bool(error.retryable)
                await self._selector.record_failure(state, retryable=retryable)
                before = cursor.counts
                if not cursor.failed(retryable=retryable):
                    raise error from exc
                if on_transition is not None:
                    await on_transition(cursor.transition(before, attempt.public, error))
                await self._retry.sleep(cursor.retry_count)
        raise RuntimeError("routing terminated without a response")


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

    def __init__(
        self,
        models: Mapping[str, Any],
        start: str,
        policy: RetryPolicyConfig,
    ) -> None:
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

        return (
            self.retry_count,
            self.deployment_failover_count,
            self.model_fallback_count,
        )

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

    def failed(self, *, retryable: bool) -> bool:
        """Record a failure and report whether policy permits another attempt."""

        if not retryable:
            return False
        self.attempt += 1
        if self.attempt < self.policy.same_deployment_attempts:
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

    def _advance_model(self) -> bool:
        if self.logical_index + 1 >= len(self.chain):
            return False
        self.logical_index += 1
        self.model_fallback_count += 1
        self.excluded.clear()
        self.current = None
        self.attempt = 0
        return True

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
        metadata["routing"] = {
            "same_deployment_retries": self.retry_count,
            "deployment_failovers": self.deployment_failover_count,
            "model_fallbacks": self.model_fallback_count,
        }
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
