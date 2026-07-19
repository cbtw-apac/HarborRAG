from __future__ import annotations

import inspect
from typing import Any, Protocol, cast

from .redis_config import RedisConnectionConfig
from .sync import run_awaitable_synchronously


class SyncRedisClient(Protocol):
    """Describe the synchronous redis-py operations used by Harbor model services."""

    def get(self, name: str) -> Any:
        """Read one Redis value."""

        ...

    def set(self, name: str, value: Any, **kwargs: Any) -> Any:
        """Store one Redis value with optional expiry or NX behavior."""

        ...

    def delete(self, *names: str) -> Any:
        """Delete one or more keys."""

        ...

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        """Execute one atomic Lua script."""

        ...

    def hgetall(self, name: str) -> Any:
        """Read one Redis hash."""

        ...

    def zcount(self, name: str, min: Any, max: Any) -> Any:
        """Count sorted-set members within a score range."""

        ...

    def zrem(self, name: str, *values: Any) -> Any:
        """Remove sorted-set members."""

        ...

    def close(self) -> Any:
        """Close the Redis client and owned pool."""

        ...


class AsyncRedisClient(Protocol):
    """Describe the asynchronous redis-py operations used by Harbor model services."""

    async def get(self, name: str) -> Any:
        """Read one Redis value asynchronously."""

        ...

    async def set(self, name: str, value: Any, **kwargs: Any) -> Any:
        """Store one Redis value asynchronously."""

        ...

    async def delete(self, *names: str) -> Any:
        """Delete one or more keys asynchronously."""

        ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        """Execute one atomic Lua script asynchronously."""

        ...

    async def hgetall(self, name: str) -> Any:
        """Read one Redis hash asynchronously."""

        ...

    async def zcount(self, name: str, min: Any, max: Any) -> Any:
        """Count sorted-set members within a score range asynchronously."""

        ...

    async def zrem(self, name: str, *values: Any) -> Any:
        """Remove sorted-set members asynchronously."""

        ...

    async def aclose(self) -> Any:
        """Close the asynchronous Redis client and owned pool."""

        ...


class RedisConnectionLifecycle:
    """Own or borrow paired sync and async Redis clients with idempotent shutdown."""

    def __init__(
        self,
        config: RedisConnectionConfig,
        *,
        sync_client: SyncRedisClient | None = None,
        async_client: AsyncRedisClient | None = None,
        owns_clients: bool | None = None,
    ) -> None:
        """Store lazy Redis connection configuration and optional injected clients."""

        self.config = config
        self._sync_client = sync_client
        self._async_client = async_client
        self._owns_clients = (
            sync_client is None and async_client is None if owns_clients is None else owns_clients
        )
        self._closed = False

    def sync(self) -> SyncRedisClient:
        """Return the shared synchronous Redis client, creating it lazily when needed."""

        self._ensure_open()
        if self._sync_client is None:
            self._sync_client = self._build_sync_client()
        return self._sync_client

    def async_client(self) -> AsyncRedisClient:
        """Return the shared asynchronous Redis client, creating it lazily when needed."""

        self._ensure_open()
        if self._async_client is None:
            self._async_client = self._build_async_client()
        return self._async_client

    def close(self) -> None:
        """Close owned sync and async clients without closing borrowed resources."""

        if self._closed:
            return
        self._closed = True
        if not self._owns_clients:
            return
        if self._sync_client is not None:
            result = self._sync_client.close()
            if inspect.isawaitable(result):
                run_awaitable_synchronously(result, thread_name="harbor-redis-close")
        if self._async_client is not None:
            run_awaitable_synchronously(
                self._async_client.aclose(), thread_name="harbor-redis-async-close"
            )

    async def aclose(self) -> None:
        """Close owned Redis clients through the asynchronous lifecycle boundary."""

        if self._closed:
            return
        self._closed = True
        if not self._owns_clients:
            return
        if self._async_client is not None:
            await self._async_client.aclose()
        if self._sync_client is not None:
            result = self._sync_client.close()
            if inspect.isawaitable(result):
                await result

    def _build_sync_client(self) -> SyncRedisClient:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("Redis support requires harborrag-models[redis]") from exc
        return cast(
            SyncRedisClient,
            redis.Redis.from_url(self.config.resolved_url(), **self._connection_options()),
        )

    def _build_async_client(self) -> AsyncRedisClient:
        try:
            import redis.asyncio as redis_async
        except ImportError as exc:
            raise RuntimeError("Redis support requires harborrag-models[redis]") from exc
        return cast(
            AsyncRedisClient,
            redis_async.Redis.from_url(self.config.resolved_url(), **self._connection_options()),
        )

    def _connection_options(self) -> dict[str, Any]:
        return {
            "max_connections": self.config.max_connections,
            "socket_timeout": self.config.socket_timeout_seconds,
            "socket_connect_timeout": self.config.socket_connect_timeout_seconds,
            "health_check_interval": self.config.health_check_interval_seconds,
            "decode_responses": self.config.decode_responses,
        }

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Redis connection lifecycle is closed")
