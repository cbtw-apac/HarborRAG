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


class _LeaderAbandonedError(Exception):
    """Tell followers the leader exited without a result or a shareable error."""


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

    def __init__(self, *, follower_timeout_seconds: float | None = None) -> None:
        """Create in-flight maps and an optional bound on follower waiting."""

        self._sync: dict[str, Future[BaseModel]] = {}
        self._async: dict[tuple[int, str], asyncio.Future[BaseModel]] = {}
        self._lock = RLock()
        self._timeout = follower_timeout_seconds

    def execute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], T],
        follower_loader: Callable[[], T | None],
    ) -> SingleFlightResult[T]:
        """Run the first producer and share a defensive result copy with followers.

        If the leader is abandoned (interrupted rather than failing), a waiting
        follower claims leadership and produces the result itself.
        """

        del follower_loader
        while True:
            future, leader = self._claim_sync(key)
            if leader:
                return self._lead_sync(key, future, producer)
            try:
                value = copy.deepcopy(future.result(self._timeout))
            except _LeaderAbandonedError:
                continue
            except TimeoutError as exc:
                raise SingleFlightTimeoutError(
                    "in-memory single-flight follower timed out"
                ) from exc
            return SingleFlightResult(value, True)  # type: ignore[arg-type]

    async def aexecute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], Awaitable[T]],
        follower_loader: Callable[[], Awaitable[T | None]],
    ) -> SingleFlightResult[T]:
        """Run the first async producer and share its defensive result copy.

        A cancelled leader re-raises its own cancellation; followers are not
        cancelled and one of them becomes the new leader.
        """

        del follower_loader
        loop = asyncio.get_running_loop()
        loop_key = (id(loop), key)
        while True:
            future, leader = self._claim_async(loop, loop_key)
            if leader:
                return await self._lead_async(loop_key, future, producer)
            try:
                value = copy.deepcopy(await asyncio.wait_for(asyncio.shield(future), self._timeout))
            except _LeaderAbandonedError:
                continue
            except TimeoutError as exc:
                raise SingleFlightTimeoutError(
                    "in-memory single-flight follower timed out"
                ) from exc
            return SingleFlightResult(value, True)  # type: ignore[arg-type]

    def _claim_sync(self, key: str) -> tuple[Future[BaseModel], bool]:
        with self._lock:
            future = self._sync.get(key)
            if future is not None:
                return future, False
            future = Future()
            self._sync[key] = future
            return future, True

    def _claim_async(
        self, loop: asyncio.AbstractEventLoop, loop_key: tuple[int, str]
    ) -> tuple[asyncio.Future[BaseModel], bool]:
        with self._lock:
            future = self._async.get(loop_key)
            if future is not None:
                return future, False
            future = loop.create_future()
            self._async[loop_key] = future
            return future, True

    def _lead_sync[T: BaseModel](
        self, key: str, future: Future[BaseModel], producer: Callable[[], T]
    ) -> SingleFlightResult[T]:
        try:
            value = producer()
        except Exception as exc:
            future.set_exception(exc)
            self._release(self._sync, key, future)
            raise
        except BaseException:
            # Release before signalling so a woken follower claims a fresh flight.
            self._release(self._sync, key, future)
            future.set_exception(_LeaderAbandonedError())
            raise
        future.set_result(value)
        self._release(self._sync, key, future)
        return SingleFlightResult(value, False)

    async def _lead_async[T: BaseModel](
        self,
        loop_key: tuple[int, str],
        future: asyncio.Future[BaseModel],
        producer: Callable[[], Awaitable[T]],
    ) -> SingleFlightResult[T]:
        try:
            value = await producer()
        except Exception as exc:
            future.set_exception(exc)
            self._release(self._async, loop_key, future)
            raise
        except BaseException:
            # Cancellation belongs to the leader alone: remove the flight first so a
            # woken follower claims a fresh one, then signal abandonment.
            self._release(self._async, loop_key, future)
            future.set_exception(_LeaderAbandonedError())
            future.exception()  # mark retrieved; there may be no follower to read it
            raise
        future.set_result(value)
        self._release(self._async, loop_key, future)
        return SingleFlightResult(value, False)

    def _release[K, F](self, flights: dict[K, F], key: K, future: F) -> None:
        with self._lock:
            if flights.get(key) is future:
                del flights[key]

    def close(self) -> None:
        """Cancel unfinished followers and clear all in-flight maps."""

        with self._lock:
            for future in self._sync.values():
                future.cancel()
            self._sync.clear()
            for async_future in self._async.values():
                async_future.cancel()
            self._async.clear()

    async def aclose(self) -> None:
        """Cancel unfinished followers through the asynchronous boundary."""

        self.close()


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
