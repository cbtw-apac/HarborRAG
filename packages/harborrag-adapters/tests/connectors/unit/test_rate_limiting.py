"""Connector rate-limit identity and failover behavior."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from harborrag_adapters.connectors.rate_limiting import (
    LocalIntervalRateLimiter,
    RateLimitIdentity,
    RedisTokenBucketRateLimiter,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class FakeRedis:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls: list[tuple[object, ...]] = []
        self.closed = False

    def eval(
        self,
        script: str,
        number_of_keys: int,
        *keys_and_args: object,
    ) -> object:
        self.calls.append((script, number_of_keys, *keys_and_args))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


def _identity() -> RateLimitIdentity:
    return RateLimitIdentity.from_http_source(
        connector_type="confluence",
        deployment_type="cloud",
        base_url="https://docs.example.com/wiki",
        credential_parts=("operator@example.com", "top-secret-token"),
    )


def test_rate_limit_key_contains_scope_dimensions_without_credentials() -> None:
    key = _identity().scope("rest").redis_key("harborrag-rate")

    assert key.startswith("harborrag-rate:confluence:cloud:docs.example.com:")
    assert key.endswith(":rest")
    assert "operator@example.com" not in key
    assert "top-secret-token" not in key


def test_local_fallback_reserves_independent_api_family_slots() -> None:
    sleeps: list[float] = []
    observed: list[tuple[object, float]] = []
    limiter = LocalIntervalRateLimiter(
        sleeper=sleeps.append,
        clock=lambda: 100.0,
        on_wait=lambda scope, duration: observed.append((scope, duration)),
    )
    identity = _identity()

    limiter.acquire(identity.scope("rest"), requests_per_minute=60)
    limiter.acquire(identity.scope("rest"), requests_per_minute=60)
    limiter.acquire(identity.scope("attachment"), requests_per_minute=60)

    assert sleeps == [1.0]
    assert observed == [(identity.scope("rest"), 1.0)]


def test_redis_token_bucket_waits_and_retries_same_secret_safe_key() -> None:
    redis = FakeRedis([[0, 25], [1, 0]])
    sleeps: list[float] = []
    observed: list[tuple[object, float]] = []
    limiter = RedisTokenBucketRateLimiter(
        redis,
        key_prefix="harborrag-rate",
        sleeper=sleeps.append,
        on_wait=lambda scope, duration: observed.append((scope, duration)),
    )

    limiter.acquire(_identity().scope("rest"), requests_per_minute=120)

    assert sleeps == [0.025]
    assert observed == [(_identity().scope("rest"), 0.025)]
    assert len(redis.calls) == 2
    first_call = redis.calls[0]
    assert first_call[1] == 1
    assert first_call[2] == redis.calls[1][2]
    assert first_call[2] == _identity().scope("rest").redis_key("harborrag-rate")
    assert first_call[3:] == ("120", "0.002", "120000")


def test_redis_loss_uses_local_fallback_without_losing_future_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = FakeRedis([ConnectionError("private endpoint"), [1, 0]])
    fallback_calls: list[tuple[Any, int]] = []

    class RecordingFallback:
        def acquire(self, scope: Any, *, requests_per_minute: int) -> None:
            fallback_calls.append((scope, requests_per_minute))

        def close(self) -> None:
            return None

    limiter = RedisTokenBucketRateLimiter(redis, fallback=RecordingFallback())
    scope = _identity().scope("rest")

    with caplog.at_level(logging.WARNING):
        limiter.acquire(scope, requests_per_minute=60)
        limiter.acquire(scope, requests_per_minute=60)

    assert fallback_calls == [(scope, 60)]
    assert "private endpoint" not in caplog.text
    assert "local fallback" in caplog.text


def test_closing_redis_limiter_closes_owned_client() -> None:
    redis = FakeRedis([[1, 0]])
    limiter = RedisTokenBucketRateLimiter(redis)

    limiter.close()

    assert redis.closed is True
