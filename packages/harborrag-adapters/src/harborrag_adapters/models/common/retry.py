from __future__ import annotations

import asyncio
import random
import time

from .config import RetryPolicyConfig


class RetryController:
    """Calculate bounded exponential-backoff delays and perform async sleeps."""

    def __init__(
        self, config: RetryPolicyConfig, *, random_source: random.Random | None = None
    ) -> None:
        """Store the retry policy and the random source used to jitter delays."""
        self.config = config
        self._random = random_source or random.Random()

    def delay_seconds(self, retry_index: int) -> float:
        """Compute the jittered exponential-backoff delay for the given retry attempt."""
        base = min(
            self.config.max_delay_seconds,
            self.config.base_delay_seconds * (2 ** max(0, retry_index - 1)),
        )
        jitter = base * self.config.jitter_ratio
        return float(max(0.0, base + self._random.uniform(-jitter, jitter)))

    async def sleep(self, retry_index: int) -> None:
        """Sleep for the computed backoff delay of the given retry attempt."""
        delay = self.delay_seconds(retry_index)
        if delay:
            await asyncio.sleep(delay)

    def sleep_sync(self, retry_index: int) -> None:
        """Sleep synchronously for the computed retry delay."""

        delay = self.delay_seconds(retry_index)
        if delay:
            time.sleep(delay)
