from __future__ import annotations

from datetime import timedelta

import pytest

from harborrag_adapters.sources._kit.rate_limit import TokenBucket
from harborrag_adapters.sources._kit.retry import RetryPolicy, parse_retry_after
from harborrag_core.sources import RateLimited

from .kit_helpers import NOW


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("2.5", 2.5),
        ("Mon, 12 Oct 2026 09:00:30 GMT", 30.0),
        ("Mon, 12 Oct 2026 08:00:00 GMT", 0.0),  # already past: retry now
        ("-3", None),
        ("nan", None),
        ("soon", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value, NOW) == expected


def test_backoff_is_full_jitter_and_capped() -> None:
    top = RetryPolicy(base_delay=0.5, max_delay=4.0, rng=lambda: 1.0)
    assert [top.backoff(attempt) for attempt in range(1, 6)] == [0.5, 1.0, 2.0, 4.0, 4.0]
    assert RetryPolicy(rng=lambda: 0.0).backoff(3) == 0.0


def test_delay_prefers_server_wait_and_escalates_long_waits() -> None:
    policy = RetryPolicy(max_retry_after=60, rng=lambda: 1.0)
    assert policy.delay(1, {"retry-after": "12"}, NOW) == 12.0
    assert policy.delay(2, {"retry-after": "garbage"}, NOW) == 1.0
    with pytest.raises(RateLimited) as caught:
        policy.delay(1, {"retry-after": "600"}, NOW)
    assert caught.value.retry_at == NOW + timedelta(seconds=600)


def test_throttled_delay_reads_the_reset_header_then_a_floor() -> None:
    policy = RetryPolicy(max_retry_after=120, min_rate_limit_delay=60, rng=lambda: 0.1)
    reset = str(int((NOW + timedelta(seconds=90)).timestamp()))
    assert policy.delay(1, {"x-ratelimit-reset": reset}, NOW, throttled=True) == 90.0
    assert policy.delay(1, {}, NOW, throttled=True) == 60.0
    assert policy.delay(1, {"x-ratelimit-reset": reset}, NOW) == pytest.approx(0.05)


def test_only_idempotent_requests_are_retryable() -> None:
    assert RetryPolicy.is_idempotent("get")
    assert not RetryPolicy.is_idempotent("POST")
    assert RetryPolicy.is_idempotent("POST", read_only=True)
    assert RetryPolicy.retryable_status(503) and not RetryPolicy.retryable_status(400)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.mark.asyncio
async def test_token_bucket_spends_burst_then_waits_for_refill() -> None:
    clock = _Clock()
    bucket = TokenBucket(2.0, 3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await bucket.acquire()
    assert clock.slept == []
    await bucket.acquire()
    assert clock.slept == [pytest.approx(0.5)]
    await bucket.acquire(2)
    assert clock.slept[-1] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        await bucket.acquire(4)


def test_hourly_bucket_bursts_a_minute_share() -> None:
    bucket = TokenBucket.per_hour(5000)
    assert bucket._burst == 83  # noqa: SLF001 - checks the derived capacity
