"""Per-credential request rate and in-flight limits for ``/mcp`` and ``/api/tools``.

One limiter serves both paths, so a credential cannot double its budget by
splitting calls between them. Budgets live in this process only. The status page,
``/healthz`` and calls without an access token (stdio) are never limited.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from harborrag_mcp_server.server.http_auth import OwnerHandler, credential_id
from harborrag_mcp_server.server.http_responses import error_response

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response

DEFAULT_REQUESTS_PER_SECOND = 10
DEFAULT_MAX_IN_FLIGHT = 4
RATE_LIMIT_VARIABLE = "HARBORRAG_MCP_RATE_LIMIT"
MAX_IN_FLIGHT_VARIABLE = "HARBORRAG_MCP_MAX_INFLIGHT"
_IN_FLIGHT_RETRY_SECONDS = 1
_CREDENTIAL_CAP = 10_000


@dataclass(frozen=True, slots=True)
class RequestLimits:
    """The budget each credential gets; 0 turns that limit off.

    Attributes:
        requests_per_second: Sustained requests per second, which is also the burst size.
        max_in_flight: Requests one credential may have running at once.
    """

    requests_per_second: int = DEFAULT_REQUESTS_PER_SECOND
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT

    def __post_init__(self) -> None:
        if self.requests_per_second < 0 or self.max_in_flight < 0:
            raise ValueError("MCP request limits must be 0 (off) or a positive integer")


def request_limits_from_environment(environment: Mapping[str, str]) -> RequestLimits:
    """Read the per-credential limits from the launch environment.

    Args:
        environment: The process environment after the launch env files are loaded.

    Returns:
        The configured limits, with the defaults for any variable left unset.

    Raises:
        ValueError: A variable is not a non-negative integer.
    """
    return RequestLimits(
        requests_per_second=_count(environment, RATE_LIMIT_VARIABLE, DEFAULT_REQUESTS_PER_SECOND),
        max_in_flight=_count(environment, MAX_IN_FLIGHT_VARIABLE, DEFAULT_MAX_IN_FLIGHT),
    )


def _count(environment: Mapping[str, str], variable: str, default: int) -> int:
    value = environment.get(variable, "").strip()
    if not value:
        return default
    try:
        count = int(value)
    except ValueError:
        count = -1
    if count < 0:
        raise ValueError(f"{variable} must be 0 (off) or a positive integer")
    return count


class RequestLimitExceeded(Exception):
    """A credential is over its request rate or has too many requests running.

    Args:
        retry_after_seconds: Whole seconds the caller should wait, at least 1.

    Attributes:
        retry_after_seconds: Whole seconds the caller should wait, at least 1.
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"request limit exceeded; retry after {retry_after_seconds} s")
        self.retry_after_seconds = retry_after_seconds


@dataclass(slots=True)
class _Allowance:
    tokens: float
    refilled_at: float
    in_flight: int = 0


class CredentialLimiter:
    """A token bucket and an in-flight cap for each credential.

    A request over either limit is refused at once rather than queued, as the HTTP
    API does, so a flooding client cannot hold connections open.

    Args:
        limits: The budget each credential gets.
        clock: Monotonic time in seconds.
    """

    def __init__(
        self, limits: RequestLimits, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._allowances: dict[str, _Allowance] = {}

    @asynccontextmanager
    async def admit(self, credential: str) -> AsyncIterator[None]:
        """Count one request against ``credential`` while the body runs.

        Args:
            credential: The ID of the credential the request authenticated with.

        Yields:
            Control while the request runs.

        Raises:
            RequestLimitExceeded: The credential is over its rate or in-flight limit.
        """
        allowance = self._allowance(credential)
        self._charge(allowance)
        allowance.in_flight += 1
        try:
            yield
        finally:
            allowance.in_flight -= 1

    def _charge(self, allowance: _Allowance) -> None:
        """Take one token, or raise when the in-flight cap or the bucket refuses."""
        cap = self._limits.max_in_flight
        if cap and allowance.in_flight >= cap:
            raise RequestLimitExceeded(_IN_FLIGHT_RETRY_SECONDS)
        rate = self._limits.requests_per_second
        if not rate:
            return
        self._refill(allowance)
        if allowance.tokens < 1:
            raise RequestLimitExceeded(max(1, math.ceil((1 - allowance.tokens) / rate)))
        allowance.tokens -= 1

    def _refill(self, allowance: _Allowance) -> None:
        now = self._clock()
        rate = self._limits.requests_per_second
        allowance.tokens = min(rate, allowance.tokens + (now - allowance.refilled_at) * rate)
        allowance.refilled_at = now

    def _allowance(self, credential: str) -> _Allowance:
        allowance = self._allowances.get(credential)
        if allowance is None:
            if len(self._allowances) >= _CREDENTIAL_CAP:
                self._prune()
            allowance = _Allowance(
                tokens=self._limits.requests_per_second, refilled_at=self._clock()
            )
            self._allowances[credential] = allowance
        return allowance

    def _prune(self) -> None:
        """Forget credentials with nothing running and a full bucket."""
        for credential, allowance in list(self._allowances.items()):
            self._refill(allowance)
            if allowance.in_flight == 0 and allowance.tokens >= self._limits.requests_per_second:
                del self._allowances[credential]


def limit_mcp_requests(server: FastMCP[Any], limiter: CredentialLimiter) -> None:
    """Refuse MCP requests over their credential's budget with FastMCP's rate-limit error.

    Every JSON-RPC request on ``/mcp`` counts, and the slot is held only while
    the request is handled, never for a client's standing event stream.

    Args:
        server: The FastMCP server whose requests are limited.
        limiter: The limiter shared with the ``/api/tools`` routes.
    """
    from fastmcp.server.dependencies import get_access_token
    from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
    from fastmcp.server.middleware.rate_limiting import RateLimitError

    class RequestLimitMiddleware(Middleware):
        async def on_request(
            self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
        ) -> Any:
            access = get_access_token()
            if access is None:
                return await call_next(context)
            try:
                async with limiter.admit(credential_id(access)):
                    return await call_next(context)
            except RequestLimitExceeded as exc:
                raise RateLimitError(str(exc)) from exc

    server.add_middleware(RequestLimitMiddleware())


def limit_requests(limiter: CredentialLimiter) -> Callable[[OwnerHandler], OwnerHandler]:
    """Answer 429 with ``Retry-After`` when an authenticated route's caller is over budget.

    Apply it beneath ``reader_or_owner``, which records the credential on the request.

    Args:
        limiter: The limiter shared with ``/mcp``.

    Returns:
        A decorator for an authenticated route handler.
    """

    def decorate(handler: OwnerHandler) -> OwnerHandler:
        async def limited(request: Request, principal_id: str) -> Response:
            try:
                async with limiter.admit(request.state.credential_id):
                    return await handler(request, principal_id)
            except RequestLimitExceeded as exc:
                return error_response(
                    str(exc),
                    status_code=429,
                    headers={"Retry-After": str(exc.retry_after_seconds)},
                )

        return limited

    return decorate
