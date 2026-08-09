"""Production composition for disposable connector rate-limit coordination."""

from __future__ import annotations

from collections.abc import Callable

from harborrag_adapters.connectors.rate_limiting import (
    ConnectorRateLimiter,
    LocalIntervalRateLimiter,
    RateLimitScope,
    RedisTokenBucketRateLimiter,
)

from .config.settings import RuntimeSettings


def build_connector_rate_limiter(
    settings: RuntimeSettings,
    *,
    on_wait: Callable[[RateLimitScope, float], None] | None = None,
) -> ConnectorRateLimiter:
    """Build Redis coordination when configured, otherwise use local pacing."""

    if settings.redis_url is None:
        return LocalIntervalRateLimiter(on_wait=on_wait)

    from redis import Redis

    client = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=30,
    )
    return RedisTokenBucketRateLimiter(
        client,
        key_prefix=settings.connector_rate_limit_key_prefix,
        on_wait=on_wait,
    )
