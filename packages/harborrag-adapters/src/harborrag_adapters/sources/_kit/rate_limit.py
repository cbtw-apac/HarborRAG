"""Client-side request budgets.

One :class:`TokenBucket` is shared by every request of one connection, so a
connector's concurrent sub-fetches cannot outrun the source's limits. A
distributed limiter (Redis sliding window) can replace it later behind the
same :class:`RateLimiter` protocol.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol


class RateLimiter(Protocol):
    async def acquire(self, cost: int = 1) -> None:
        """Wait until ``cost`` request units may be spent."""
        ...


class UnlimitedRateLimiter:
    """No client-side limit; the source's own throttling still applies."""

    async def acquire(self, cost: int = 1) -> None:
        del cost


class TokenBucket:
    """Classic token bucket: ``rate_per_second`` refill, ``burst`` capacity."""

    def __init__(
        self,
        rate_per_second: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        self._rate = rate_per_second
        self._burst = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._updated = clock()
        self._lock = asyncio.Lock()

    @classmethod
    def per_hour(cls, requests: int, *, burst: int | None = None) -> TokenBucket:
        """A bucket for an hourly budget, bursting up to a minute's share by default."""

        if requests < 1:
            raise ValueError("hourly budget must be positive")
        return cls(requests / 3600.0, burst or max(1, requests // 60))

    async def acquire(self, cost: int = 1) -> None:
        if cost < 1 or cost > self._burst:
            raise ValueError(f"cost must be between 1 and the burst size {self._burst:g}")
        async with self._lock:
            self._refill()
            if self._tokens < cost:
                await self._sleep((cost - self._tokens) / self._rate)
                self._refill()
            self._tokens -= cost

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._updated = now
