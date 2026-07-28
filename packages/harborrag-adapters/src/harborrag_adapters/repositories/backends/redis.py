from __future__ import annotations

from typing import Any, Protocol

from harborrag_adapters.repositories.lifecycle import AsyncLifecycle

redis: Any
try:
    import redis.asyncio as _redis_asyncio
except ImportError:  # pragma: no cover - optional dependency
    redis = None
else:
    redis = _redis_asyncio


class RedisClientProtocol(Protocol):
    """Describes the Redis lifecycle required by cache repository compositions."""

    @property
    def backend(self) -> str: ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def raw(self) -> Any: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> None: ...


class RedisDBClient(AsyncLifecycle):
    """Owns one asynchronous Redis connection for a repository family."""

    def __init__(
        self,
        *,
        url: str,
        connect_timeout_seconds: float,
        operation_timeout_seconds: float,
    ) -> None:
        if redis is None:
            raise ImportError("redis is not installed")
        self._url = url
        self._connect_timeout_seconds = connect_timeout_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._client: Any = None

    @property
    def backend(self) -> str:
        return "redis"

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def raw(self) -> Any:
        if self._client is None:
            raise RuntimeError("Redis database client is not connected")
        return self._client

    async def connect(self) -> None:
        if self._client is None:
            self._client = redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=self._connect_timeout_seconds,
                socket_timeout=self._operation_timeout_seconds,
            )
            try:
                await self._client.ping()
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> None:
        await self.raw.ping()
