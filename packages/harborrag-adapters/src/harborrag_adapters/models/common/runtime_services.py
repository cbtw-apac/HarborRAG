from __future__ import annotations

from dataclasses import dataclass

from .budget import InMemoryBudgetPolicy, ModelBudgetPolicy, NoopBudgetPolicy
from .cache import ModelResponseCache
from .cache_redis import RedisModelCache
from .config import CacheBackend, ModelClientConfig
from .distributed_config import RoutingStateBackend, SingleFlightBackend
from .redis_client import RedisConnectionLifecycle
from .routing_state import RoutingStateStore
from .routing_state_memory import InMemoryRoutingStateStore
from .routing_state_redis import RedisRoutingStateStore
from .singleflight import (
    InMemorySingleFlight,
    NoopSingleFlight,
    RedisSingleFlight,
    SingleFlightCoordinator,
)


@dataclass(slots=True)
class ModelRuntimeServices:
    """Group distributed runtime services while retaining explicit ownership."""

    cache: ModelResponseCache | None
    routing_state: RoutingStateStore
    singleflight: SingleFlightCoordinator
    budget: ModelBudgetPolicy
    redis: RedisConnectionLifecycle | None = None

    def close(self) -> None:
        """Close runtime services and the shared Redis lifecycle exactly once."""

        self.routing_state.close()
        self.singleflight.close()
        if self.redis is not None:
            self.redis.close()

    async def aclose(self) -> None:
        """Close runtime services asynchronously in deterministic order."""

        await self.routing_state.aclose()
        await self.singleflight.aclose()
        if self.redis is not None:
            await self.redis.aclose()


def build_runtime_services(
    config: ModelClientConfig,
    *,
    family: str,
    cache: ModelResponseCache | None = None,
    routing_state: RoutingStateStore | None = None,
    singleflight: SingleFlightCoordinator | None = None,
    budget: ModelBudgetPolicy | None = None,
    redis: RedisConnectionLifecycle | None = None,
) -> ModelRuntimeServices:
    """Build configured shared services without creating module-level mutable state."""

    lifecycle = redis
    redis_needed = (
        config.cache.backend is CacheBackend.REDIS
        or config.routing.state_backend is RoutingStateBackend.REDIS
        or config.singleflight.backend is SingleFlightBackend.REDIS
    )
    if redis_needed and lifecycle is None:
        if config.redis is None:
            raise ValueError("Redis-backed services require redis configuration")
        lifecycle = RedisConnectionLifecycle(config.redis)
    resolved_cache = cache
    if resolved_cache is None and config.cache.backend is CacheBackend.REDIS:
        assert lifecycle is not None
        resolved_cache = RedisModelCache(
            lifecycle,
            key_prefix=config.redis.key_prefix if config.redis else "harborrag:models",
        )
    resolved_routing = routing_state or _routing_state(config, lifecycle)
    resolved_singleflight = singleflight or _singleflight(config, lifecycle)
    resolved_budget = budget or (
        InMemoryBudgetPolicy(config.budget) if config.budget.enabled else NoopBudgetPolicy()
    )
    return ModelRuntimeServices(
        cache=resolved_cache,
        routing_state=resolved_routing,
        singleflight=resolved_singleflight,
        budget=resolved_budget,
        redis=lifecycle if redis is None else None,
    )


def _routing_state(
    config: ModelClientConfig, lifecycle: RedisConnectionLifecycle | None
) -> RoutingStateStore:
    if config.routing.state_backend is RoutingStateBackend.MEMORY:
        return InMemoryRoutingStateStore()
    assert lifecycle is not None and config.redis is not None
    return RedisRoutingStateStore(lifecycle, key_prefix=config.redis.key_prefix)


def _singleflight(
    config: ModelClientConfig, lifecycle: RedisConnectionLifecycle | None
) -> SingleFlightCoordinator:
    settings = config.singleflight
    if settings.backend is SingleFlightBackend.DISABLED:
        return NoopSingleFlight()
    if settings.backend is SingleFlightBackend.MEMORY:
        return InMemorySingleFlight()
    assert lifecycle is not None and config.redis is not None
    return RedisSingleFlight(
        lifecycle,
        key_prefix=config.redis.key_prefix,
        lock_ttl_seconds=settings.lock_ttl_seconds,
        follower_timeout_seconds=settings.follower_timeout_seconds,
        poll_interval_seconds=settings.poll_interval_seconds,
    )
