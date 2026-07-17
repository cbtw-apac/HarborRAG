from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .distributed_config import BudgetPolicyConfig


class BudgetExceededError(RuntimeError):
    """Report that a request exceeds configured token, rate, or spend limits."""


class BudgetEstimationError(RuntimeError):
    """Report that a cost limit cannot be enforced without a cost estimator."""


class BudgetAuthorization(BaseModel):
    """Describe one accepted budget reservation for later usage settlement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    scope: str
    estimated_tokens: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class ModelBudgetPolicy(Protocol):
    """Define portable request authorization and actual-usage settlement."""

    def authorize(self, request: BaseModel, *, logical_model: str) -> BudgetAuthorization:
        """Authorize one synchronous model request before provider execution."""

        ...

    async def aauthorize(self, request: BaseModel, *, logical_model: str) -> BudgetAuthorization:
        """Authorize one asynchronous model request before provider execution."""

        ...

    def settle(self, authorization: BudgetAuthorization, response: BaseModel) -> None:
        """Settle actual usage and cost after a successful response."""

        ...

    async def asettle(self, authorization: BudgetAuthorization, response: BaseModel) -> None:
        """Settle actual usage and cost asynchronously."""

        ...


class NoopBudgetPolicy:
    """Accept every request while preserving a stable policy boundary."""

    def authorize(self, request: BaseModel, *, logical_model: str) -> BudgetAuthorization:
        """Return a zero-cost authorization without tracking mutable state."""

        del request
        return BudgetAuthorization(id=uuid.uuid4().hex, scope=logical_model, estimated_tokens=0)

    async def aauthorize(self, request: BaseModel, *, logical_model: str) -> BudgetAuthorization:
        """Return a zero-cost authorization asynchronously."""

        return self.authorize(request, logical_model=logical_model)

    def settle(self, authorization: BudgetAuthorization, response: BaseModel) -> None:
        """Ignore usage because budgeting is disabled."""

        del authorization, response

    async def asettle(self, authorization: BudgetAuthorization, response: BaseModel) -> None:
        """Ignore asynchronous usage because budgeting is disabled."""

        self.settle(authorization, response)


@dataclass(slots=True)
class _BudgetWindow:
    minute: int = -1
    requests: int = 0
    tokens: int = 0
    day: str = ""
    day_cost: float = 0.0
    month: str = ""
    month_cost: float = 0.0


class InMemoryBudgetPolicy:
    """Enforce tenant-scoped request, rate, and spend limits in one process."""

    def __init__(
        self,
        config: BudgetPolicyConfig,
        *,
        cost_estimator: Callable[[BaseModel, str], float | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Store immutable limits and optional model-aware cost estimation."""

        self.config = config
        self._cost_estimator = cost_estimator
        self._clock = clock
        self._windows: dict[str, _BudgetWindow] = {}
        self._lock = RLock()

    def authorize(self, request: BaseModel, *, logical_model: str) -> BudgetAuthorization:
        """Reserve request and estimated tokens under the configured tenant scope."""

        scope = self._scope(request, logical_model)
        estimated_tokens = estimate_request_tokens(request)
        estimated_cost = self._estimate_cost(request, logical_model)
        self._validate_request(estimated_tokens, estimated_cost)
        with self._lock:
            window = self._window(scope)
            self._reset_windows(window)
            if self.config.rpm_limit is not None and window.requests >= self.config.rpm_limit:
                raise BudgetExceededError("request rate budget exceeded")
            if (
                self.config.tpm_limit is not None
                and window.tokens + estimated_tokens > self.config.tpm_limit
            ):
                raise BudgetExceededError("token rate budget exceeded")
            window.requests += 1
            window.tokens += estimated_tokens
        return BudgetAuthorization(
            id=uuid.uuid4().hex,
            scope=scope,
            estimated_tokens=estimated_tokens,
            estimated_cost_usd=estimated_cost,
        )

    async def aauthorize(self, request: BaseModel, *, logical_model: str) -> BudgetAuthorization:
        """Authorize an asynchronous request using thread-safe in-memory state."""

        return self.authorize(request, logical_model=logical_model)

    def settle(self, authorization: BudgetAuthorization, response: BaseModel) -> None:
        """Apply actual response cost to daily and monthly spend windows."""

        cost = _response_cost(response)
        if cost is None:
            return
        with self._lock:
            window = self._window(authorization.scope)
            self._reset_windows(window)
            if (
                self.config.daily_cost_usd is not None
                and window.day_cost + cost > self.config.daily_cost_usd
            ):
                raise BudgetExceededError("daily cost budget exceeded during settlement")
            if (
                self.config.monthly_cost_usd is not None
                and window.month_cost + cost > self.config.monthly_cost_usd
            ):
                raise BudgetExceededError("monthly cost budget exceeded during settlement")
            window.day_cost += cost
            window.month_cost += cost

    async def asettle(self, authorization: BudgetAuthorization, response: BaseModel) -> None:
        """Settle asynchronous response usage using thread-safe state."""

        self.settle(authorization, response)

    def snapshot(self, scope: str) -> dict[str, float | int | str]:
        """Return sanitized current counters for runtime introspection."""

        with self._lock:
            window = self._window(scope)
            self._reset_windows(window)
            return {
                "minute": window.minute,
                "requests": window.requests,
                "tokens": window.tokens,
                "day": window.day,
                "day_cost_usd": window.day_cost,
                "month": window.month,
                "month_cost_usd": window.month_cost,
            }

    def _scope(self, request: BaseModel, logical_model: str) -> str:
        metadata = getattr(request, "metadata", None)
        tenant_id = getattr(metadata, "tenant_id", None)
        if self.config.require_tenant_id and not tenant_id:
            raise BudgetExceededError("tenant_id is required by budget policy")
        return f"{tenant_id or '__unscoped__'}:{logical_model}"

    def _estimate_cost(self, request: BaseModel, logical_model: str) -> float | None:
        if self._cost_estimator is None:
            if self.config.max_request_cost_usd is not None:
                raise BudgetEstimationError("max_request_cost_usd requires a cost estimator")
            return None
        return self._cost_estimator(request, logical_model)

    def _validate_request(self, tokens: int, cost: float | None) -> None:
        if self.config.max_request_tokens is not None and tokens > self.config.max_request_tokens:
            raise BudgetExceededError("request token budget exceeded")
        if (
            self.config.max_request_cost_usd is not None
            and cost is not None
            and cost > self.config.max_request_cost_usd
        ):
            raise BudgetExceededError("request cost budget exceeded")

    def _window(self, scope: str) -> _BudgetWindow:
        return self._windows.setdefault(scope, _BudgetWindow())

    def _reset_windows(self, window: _BudgetWindow) -> None:
        now = datetime.fromtimestamp(self._clock(), UTC)
        minute = int(self._clock() // 60)
        if window.minute != minute:
            window.minute = minute
            window.requests = 0
            window.tokens = 0
        day = now.strftime("%Y-%m-%d")
        if window.day != day:
            window.day = day
            window.day_cost = 0.0
        month = now.strftime("%Y-%m")
        if window.month != month:
            window.month = month
            window.month_cost = 0.0


def estimate_request_tokens(request: BaseModel) -> int:
    """Estimate request tokens conservatively from explicit budget or serialized content."""

    explicit = getattr(request, "token_budget", None)
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    payload = json.dumps(request.model_dump(mode="json"), sort_keys=True, default=str)
    return max(1, (len(payload) + 3) // 4)


def _response_cost(response: BaseModel) -> float | None:
    value: Any = getattr(response, "estimated_cost_usd", None)
    return float(value) if isinstance(value, int | float) and value >= 0 else None
