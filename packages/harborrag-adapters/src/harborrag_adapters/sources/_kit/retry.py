"""The one retry policy every source connector shares.

Only idempotent requests are retried: GET, HEAD, and POSTs the caller marks
``read_only`` (JQL search, bulk fetch). Backoff is exponential with full
jitter. A server-requested wait is honoured up to ``max_retry_after``; a
longer one raises :class:`RateLimited` so the runtime reschedules the run
instead of blocking a worker.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from harborrag_core.sources import RateLimited

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds to wait from a ``Retry-After`` value (delta or HTTP-date).

    A date in the past means "now" (0). Anything unreadable, or a negative
    delta, is ignored (``None``) so the caller falls back to computed backoff.
    """

    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        return _seconds_until_date(text, now)
    if seconds < 0 or seconds != seconds:  # NaN never equals itself
        return None
    return seconds


def _seconds_until_date(text: str, now: datetime) -> float | None:
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - now).total_seconds())


def _seconds_until_reset(value: str | None, now: datetime) -> float | None:
    """``X-RateLimit-Reset`` as epoch seconds (GitHub, GitLab)."""

    if value is None:
        return None
    try:
        reset = float(value.strip())
    except ValueError:
        return None
    return max(0.0, reset - now.timestamp())


@dataclass(frozen=True)
class RetryPolicy:
    """Attempts, backoff, and how long a server may make us wait in-process."""

    attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    max_retry_after: float = 60.0
    min_rate_limit_delay: float = 0.0
    """Floor for a throttled response that names no wait (GitHub secondary limits)."""
    rng: Callable[[], float] = field(default=random.random, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if min(self.base_delay, self.max_delay, self.max_retry_after) < 0:
            raise ValueError("delays must not be negative")

    @staticmethod
    def is_idempotent(method: str, *, read_only: bool = False) -> bool:
        return read_only or method.upper() in _IDEMPOTENT_METHODS

    @staticmethod
    def retryable_status(status_code: int) -> bool:
        return status_code in _RETRYABLE_STATUS

    def backoff(self, attempt: int) -> float:
        """Full-jitter exponential backoff for the ``attempt``-th retry (1-based)."""

        ceiling = min(self.max_delay, self.base_delay * 2.0 ** max(0, attempt - 1))
        return self.rng() * ceiling

    def delay(
        self,
        attempt: int,
        headers: Mapping[str, str],
        now: datetime,
        *,
        throttled: bool = False,
    ) -> float:
        """Seconds to sleep before the next attempt.

        Raises :class:`RateLimited` when the server asks for longer than
        ``max_retry_after``.
        """

        requested = parse_retry_after(headers.get("retry-after"), now)
        if requested is None and throttled:
            requested = _seconds_until_reset(headers.get("x-ratelimit-reset"), now)
        if requested is None:
            computed = self.backoff(attempt)
            return max(computed, self.min_rate_limit_delay) if throttled else computed
        if requested > self.max_retry_after:
            raise RateLimited(now + timedelta(seconds=requested))
        return requested
