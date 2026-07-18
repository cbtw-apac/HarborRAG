from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from harborrag_core.models.errors import HarborModelError
from pydantic import BaseModel

from .config import RetryPolicyConfig, RoutingConfig
from .errors import HarborNoHealthyDeploymentError
from .retry import RetryController
from .routing import DeploymentLike, DeploymentSelector
from .routing_cursor import RoutingExecutionCursor
from .routing_state import RoutingAdmissionError, RoutingStateStore
from .routing_types import RoutedAttempt, RoutedResult, RoutingTransition

type ErrorNormalizer[D: DeploymentLike] = Callable[[Exception, str, D], HarborModelError]
type SyncInvoke[D: DeploymentLike] = Callable[[RoutedAttempt[D]], Any]
type AsyncInvoke[D: DeploymentLike] = Callable[[RoutedAttempt[D]], Awaitable[Any]]
type ResponseNormalizer[T: BaseModel, D: DeploymentLike] = Callable[[Any, str, D, float], T]
type SyncTransitionCallback[D: DeploymentLike] = Callable[[RoutingTransition[D]], None]
type AsyncTransitionCallback[D: DeploymentLike] = Callable[[RoutingTransition[D]], Awaitable[None]]


@dataclass(slots=True)
class RoutingRuntime[D: DeploymentLike]:
    """Hold shared selector, retry policy, and health state for one client."""

    models: Mapping[str, Any]
    policy: RetryPolicyConfig
    retry: RetryController
    selector: DeploymentSelector[D]

    @classmethod
    def build(
        cls,
        models: Mapping[str, Any],
        *,
        routing: RoutingConfig,
        retry: RetryPolicyConfig,
        state_store: RoutingStateStore | None = None,
    ) -> RoutingRuntime[D]:
        """Create routing state shared by streaming and non-streaming execution."""

        selector = DeploymentSelector(
            {name: model.deployments for name, model in models.items()},
            strategy=routing.strategy,
            circuit_breaker=routing.circuit_breaker,
            enable_health_tracking=routing.enable_health_tracking,
            state_store=state_store,
            lease_seconds=routing.lease_seconds,
            health_stale_seconds=routing.active_health.stale_after_seconds,
        )
        return cls(models, retry, RetryController(retry), selector)

    def cursor(self, logical_model: str) -> RoutingExecutionCursor[D]:
        """Create an independent bounded cursor over shared deployment health state."""

        return RoutingExecutionCursor(self.models, logical_model, self.policy)


class RoutedModelExecutor[D: DeploymentLike]:
    """Own retry and fallback transitions shared by every model family."""

    def __init__(
        self,
        models: Mapping[str, Any],
        *,
        routing: RoutingConfig,
        retry: RetryPolicyConfig,
        runtime: RoutingRuntime[D] | None = None,
        state_store: RoutingStateStore | None = None,
    ) -> None:
        """Create or reuse one routing runtime for this client."""

        self.runtime = runtime or RoutingRuntime.build(
            models, routing=routing, retry=retry, state_store=state_store
        )

    def execute[T: BaseModel](
        self,
        logical_model: str,
        *,
        invoke: SyncInvoke[D],
        normalize: ResponseNormalizer[T, D],
        normalize_error: ErrorNormalizer[D],
        on_transition: SyncTransitionCallback[D] | None = None,
        estimated_tokens: int = 0,
    ) -> RoutedResult[T, D]:
        """Execute synchronously with bounded retries and explicit fallback stages."""

        cursor = self.runtime.cursor(logical_model)
        last_error: HarborModelError | None = None
        while attempt := cursor.next_attempt_sync(self.runtime.selector):
            started = time.perf_counter()
            state = attempt.state
            try:
                with self.runtime.selector.lease_sync(
                    state,
                    logical_model=attempt.logical_model,
                    token_cost=estimated_tokens,
                ):
                    raw = invoke(attempt.public)
                latency = (time.perf_counter() - started) * 1_000
                self.runtime.selector.record_success_sync(state, latency)
                value = normalize(raw, attempt.logical_model, state.config, latency)
                return cursor.result(value, attempt.public)
            except Exception as exc:
                error = _normalize_execution_error(
                    exc, attempt.logical_model, state.config, normalize_error
                )
                last_error = error
                retryable = bool(error.retryable)
                self.runtime.selector.record_failure_sync(state, retryable=retryable)
                before = cursor.counts
                if not cursor.failed(
                    retryable=retryable,
                    current_available=state.available(time.monotonic()),
                ):
                    raise error from exc
                if on_transition is not None:
                    on_transition(cursor.transition(before, attempt.public, error))
                delay = self.runtime.retry.delay_seconds(cursor.retry_count)
                if delay:
                    time.sleep(delay)
        raise routing_unavailable_error(logical_model, cursor, last_error)

    async def aexecute[T: BaseModel](
        self,
        logical_model: str,
        *,
        invoke: AsyncInvoke[D],
        normalize: ResponseNormalizer[T, D],
        normalize_error: ErrorNormalizer[D],
        on_transition: AsyncTransitionCallback[D] | None = None,
        estimated_tokens: int = 0,
    ) -> RoutedResult[T, D]:
        """Execute asynchronously with cancellation-safe deployment concurrency."""

        cursor = self.runtime.cursor(logical_model)
        last_error: HarborModelError | None = None
        while attempt := await cursor.next_attempt(self.runtime.selector):
            started = time.perf_counter()
            state = attempt.state
            try:
                async with self.runtime.selector.lease(
                    state,
                    logical_model=attempt.logical_model,
                    token_cost=estimated_tokens,
                ):
                    raw = await invoke(attempt.public)
                latency = (time.perf_counter() - started) * 1_000
                await self.runtime.selector.record_success(state, latency)
                value = normalize(raw, attempt.logical_model, state.config, latency)
                return cursor.result(value, attempt.public)
            except Exception as exc:
                error = _normalize_execution_error(
                    exc, attempt.logical_model, state.config, normalize_error
                )
                last_error = error
                retryable = bool(error.retryable)
                await self.runtime.selector.record_failure(state, retryable=retryable)
                before = cursor.counts
                if not cursor.failed(
                    retryable=retryable,
                    current_available=state.available(time.monotonic()),
                ):
                    raise error from exc
                if on_transition is not None:
                    await on_transition(cursor.transition(before, attempt.public, error))
                await self.runtime.retry.sleep(cursor.retry_count)
        raise routing_unavailable_error(logical_model, cursor, last_error)


def _normalize_execution_error[D: DeploymentLike](
    exc: Exception,
    logical_model: str,
    deployment: D,
    normalizer: ErrorNormalizer[D],
) -> HarborModelError:
    """Normalize distributed admission failures without misclassifying provider errors."""

    if isinstance(exc, RoutingAdmissionError):
        error = HarborNoHealthyDeploymentError(
            f"deployment admission rejected by {exc.reason}",
            operation="route",
            logical_model=logical_model,
            provider_model=getattr(deployment, "model", None),
            deployment=deployment.name,
            retryable=True,
            original_exception=exc,
        )
        return error
    return normalizer(exc, logical_model, deployment)


def routing_unavailable_error(
    logical_model: str,
    cursor: RoutingExecutionCursor[Any],
    last_error: HarborModelError | None,
) -> HarborNoHealthyDeploymentError:
    """Create a stable sanitized error after all routes are exhausted."""

    error = HarborNoHealthyDeploymentError(
        "no healthy model deployment is available after routing policy exhaustion",
        operation="route",
        logical_model=logical_model,
        retryable=True,
        original_exception=last_error,
    )
    error.add_note(f"routing diagnostics: {cursor.diagnostics}")
    return error
