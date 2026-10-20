from __future__ import annotations

import logging

import pytest

from harborrag_adapters.sources._kit.rate_limit import (
    RedisTokenBucket,
    TokenBucket,
    rate_limit_key,
)

LOGGER = "harborrag.adapters.sources.kit.rate_limit"


class FakeRedis:
    """Replays scripted ``eval`` outcomes in order and records each call.

    It runs no bucket arithmetic: each outcome is the script's reply (or an
    exception to raise), so a test decides what Redis answers.
    """

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> object:
        self.calls.append((script, numkeys, keys_and_args))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class CountingFallback:
    def __init__(self) -> None:
        self.costs: list[int] = []

    async def acquire(self, cost: int = 1) -> None:
        self.costs.append(cost)


def make(
    client: FakeRedis, sleeps: Sleeps, fallback: CountingFallback | None = None
) -> RedisTokenBucket:
    return RedisTokenBucket(
        client,
        key="rate:conn:host",
        rate_per_second=4.0,
        burst=5,
        fallback=fallback or CountingFallback(),
        sleep=sleeps,
    )


@pytest.mark.asyncio
async def test_waits_the_time_redis_reports() -> None:
    client, sleeps = FakeRedis([0, 250], [1, 0]), Sleeps()
    await make(client, sleeps).acquire()
    assert sleeps.calls == [0.25]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_script_arguments_carry_key_capacity_refill_ttl_and_cost() -> None:
    client, sleeps = FakeRedis([1, 0]), Sleeps()
    await make(client, sleeps).acquire(3)
    _, numkeys, args = client.calls[0]
    assert numkeys == 1
    assert args[0] == "rate:conn:host"
    assert args[1:] == ("5", repr(4.0 / 1000), "120000", "3")


@pytest.mark.asyncio
async def test_cost_above_burst_is_rejected() -> None:
    client = FakeRedis()
    with pytest.raises(ValueError, match="cost"):
        await make(client, Sleeps()).acquire(6)
    with pytest.raises(ValueError, match="cost"):
        await make(client, Sleeps()).acquire(0)
    assert client.calls == []


def test_invalid_settings_are_rejected() -> None:
    for rate, burst in ((0.0, 5), (-1.0, 5), (1.0, 0)):
        with pytest.raises(ValueError):
            RedisTokenBucket(
                FakeRedis(),
                key="k",
                rate_per_second=rate,
                burst=burst,
                fallback=CountingFallback(),
            )


@pytest.mark.asyncio
async def test_redis_error_uses_fallback_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    secret = "redis://:hunter2@cache.internal:6379"
    client = FakeRedis(*[ConnectionError(secret) for _ in range(3)])
    fallback = CountingFallback()
    limiter = make(client, Sleeps(), fallback)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(3):
            await limiter.acquire()
    assert fallback.costs == [1, 1, 1]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "ConnectionError" in text
    assert "hunter2" not in text
    assert "rate:conn:host" not in text


@pytest.mark.asyncio
async def test_recovers_to_redis_after_outage(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeRedis(ConnectionError("x"), ConnectionError("x"), [1, 0], ConnectionError("x"))
    fallback = CountingFallback()
    limiter = make(client, Sleeps(), fallback)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire(2)
        assert fallback.costs == [1, 1]
        await limiter.acquire()  # a new outage warns again
    assert len(caplog.records) == 2


@pytest.mark.asyncio
async def test_invalid_script_reply_uses_fallback() -> None:
    fallback = CountingFallback()
    await make(FakeRedis("nope"), Sleeps(), fallback).acquire()
    assert fallback.costs == [1]


@pytest.mark.asyncio
async def test_sleep_errors_are_not_swallowed() -> None:
    class Boom(Exception):
        pass

    async def sleep(seconds: float) -> None:
        raise Boom

    fallback = CountingFallback()
    limiter = RedisTokenBucket(
        FakeRedis([0, 10]),
        key="k",
        rate_per_second=1.0,
        burst=1,
        fallback=fallback,
        sleep=sleep,
    )
    with pytest.raises(Boom):
        await limiter.acquire()
    assert fallback.costs == []


def test_rate_limit_key_is_sanitised() -> None:
    assert (
        rate_limit_key("HarborRAG Rate", "conn/42", "Jira.Example.com:8443")
        == "harborrag-rate:conn-42:jira.example.com-8443"
    )


@pytest.mark.asyncio
async def test_fallback_can_be_a_token_bucket() -> None:
    bucket_sleeps = Sleeps()
    fallback = TokenBucket(1.0, 1, clock=lambda: 0.0, sleep=bucket_sleeps)
    client = FakeRedis(ConnectionError("down"), ConnectionError("down"))
    limiter = RedisTokenBucket(client, key="k", rate_per_second=1.0, burst=1, fallback=fallback)
    await limiter.acquire()
    await limiter.acquire()
    # The second acquire found the local bucket empty and waited a full refill.
    assert bucket_sleeps.calls == [1.0]
    assert len(client.calls) == 2
