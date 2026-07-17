from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from pydantic import BaseModel

from .redis_client import RedisConnectionLifecycle


class SingleFlightTimeoutError(TimeoutError):
    """Report that a follower did not observe the leader result before its timeout."""


@dataclass(frozen=True, slots=True)
class SingleFlightResult[T: BaseModel]:
    """Return one result and whether it came from another in-flight caller."""

    value: T
    shared: bool


class SingleFlightCoordinator(Protocol):
    """Deduplicate identical synchronous and asynchronous request executions."""

    def execute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], T],
        follower_loader: Callable[[], T | None],
    ) -> SingleFlightResult[T]:
        """Run or follow one synchronous keyed operation."""

        ...

    async def aexecute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], Awaitable[T]],
        follower_loader: Callable[[], Awaitable[T | None]],
    ) -> SingleFlightResult[T]:
        """Run or follow one asynchronous keyed operation."""

        ...

    def close(self) -> None:
        """Release coordinator resources."""

        ...

    async def aclose(self) -> None:
        """Release coordinator resources asynchronously."""

        ...


class NoopSingleFlight:
    """Execute every request independently while preserving one stable boundary."""

    def execute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], T],
        follower_loader: Callable[[], T | None],
    ) -> SingleFlightResult[T]:
        """Execute the producer without coordination."""

        del key, follower_loader
        return SingleFlightResult(producer(), False)

    async def aexecute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], Awaitable[T]],
        follower_loader: Callable[[], Awaitable[T | None]],
    ) -> SingleFlightResult[T]:
        """Execute the asynchronous producer without coordination."""

        del key, follower_loader
        return SingleFlightResult(await producer(), False)

    def close(self) -> None:
        """Return because no resources are owned."""

    async def aclose(self) -> None:
        """Return because no asynchronous resources are owned."""


class InMemorySingleFlight:
    """Share one in-process result among concurrent callers of the same key."""

    def __init__(self) -> None:
        """Create independent synchronous and asynchronous in-flight maps."""

        self._sync: dict[str, Future[BaseModel]] = {}
        self._async: dict[str, asyncio.Future[BaseModel]] = {}
        self._lock = RLock()
        self._async_lock = asyncio.Lock()

    def execute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], T],
        follower_loader: Callable[[], T | None],
    ) -> SingleFlightResult[T]:
        """Run the first producer and share a defensive result copy with followers."""

        del follower_loader
        with self._lock:
            future = self._sync.get(key)
            leader = future is None
            if future is None:
                future = Future()
                self._sync[key] = future
        if not leader:
            value = copy.deepcopy(future.result())
            return SingleFlightResult(value, True)  # type: ignore[arg-type]
        try:
            value = producer()
            future.set_result(value)
            return SingleFlightResult(value, False)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._sync.pop(key, None)

    async def aexecute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], Awaitable[T]],
        follower_loader: Callable[[], Awaitable[T | None]],
    ) -> SingleFlightResult[T]:
        """Run the first async producer and share its defensive result copy."""

        del follower_loader
        async with self._async_lock:
            future = self._async.get(key)
            leader = future is None
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._async[key] = future
        if not leader:
            value = copy.deepcopy(await asyncio.shield(future))
            return SingleFlightResult(value, True)  # type: ignore[arg-type]
        try:
            value = await producer()
            future.set_result(value)
            return SingleFlightResult(value, False)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            async with self._async_lock:
                self._async.pop(key, None)

    def close(self) -> None:
        """Cancel unfinished synchronous followers."""

        with self._lock:
            for future in self._sync.values():
                future.cancel()
            self._sync.clear()

    async def aclose(self) -> None:
        """Cancel unfinished asynchronous followers and clear all maps."""

        self.close()
        async with self._async_lock:
            for future in self._async.values():
                future.cancel()
            self._async.clear()


class RedisSingleFlight:
    """Coordinate leaders with Redis locks while followers load the shared cache result."""

    def __init__(
        self,
        connections: RedisConnectionLifecycle,
        *,
        key_prefix: str,
        lock_ttl_seconds: int,
        follower_timeout_seconds: float,
        poll_interval_seconds: float,
        owns_connections: bool = False,
    ) -> None:
        """Bind Redis lock configuration and shared connection ownership."""

        self._connections = connections
        self._prefix = key_prefix.rstrip(":")
        self._lock_ttl = lock_ttl_seconds
        self._timeout = follower_timeout_seconds
        self._poll = poll_interval_seconds
        self._owns_connections = owns_connections

    def execute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], T],
        follower_loader: Callable[[], T | None],
    ) -> SingleFlightResult[T]:
        """Execute as Redis lock leader or poll the shared response cache as follower."""

        lock_key = self._key(key)
        token = uuid.uuid4().hex
        client = self._connections.sync()
        leader = bool(client.set(lock_key, token, nx=True, ex=self._lock_ttl))
        if leader:
            try:
                return SingleFlightResult(producer(), False)
            finally:
                client.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if value := follower_loader():
                return SingleFlightResult(value, True)
            time.sleep(self._poll)
        raise SingleFlightTimeoutError("Redis single-flight follower timed out")

    async def aexecute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], Awaitable[T]],
        follower_loader: Callable[[], Awaitable[T | None]],
    ) -> SingleFlightResult[T]:
        """Execute as asynchronous Redis leader or poll the shared cache as follower."""

        lock_key = self._key(key)
        token = uuid.uuid4().hex
        client = self._connections.async_client()
        leader = bool(await client.set(lock_key, token, nx=True, ex=self._lock_ttl))
        if leader:
            try:
                return SingleFlightResult(await producer(), False)
            finally:
                await client.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if value := await follower_loader():
                return SingleFlightResult(value, True)
            await asyncio.sleep(self._poll)
        raise SingleFlightTimeoutError("Redis single-flight follower timed out")

    def close(self) -> None:
        """Close Redis connections only when owned by this coordinator."""

        if self._owns_connections:
            self._connections.close()

    async def aclose(self) -> None:
        """Close owned Redis connections asynchronously."""

        if self._owns_connections:
            await self._connections.aclose()

    def _key(self, key: str) -> str:
        return f"{self._prefix}:singleflight:{key}"


_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
