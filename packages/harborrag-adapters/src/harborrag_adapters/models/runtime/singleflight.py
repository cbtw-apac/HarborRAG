from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock
from typing import Protocol

from pydantic import BaseModel

from .redis_client import AsyncRedisClient, RedisConnectionLifecycle, SyncRedisClient

_LOGGER = logging.getLogger(__name__)


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
        follower claims leadership; it first reuses a result the abandoned leader
        already cached, and only otherwise produces the result itself.
        """

        recheck: Callable[[], T | None] | None = None
        while True:
            future, leader = self._claim_sync(key)
            if leader:
                return self._lead_sync(key, future, producer, recheck)
            try:
                value = copy.deepcopy(future.result(self._timeout))
            except _LeaderAbandonedError:
                recheck = follower_loader
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
        cancelled and one of them becomes the new leader, reusing a result the
        cancelled leader already cached before producing one itself.
        """

        loop = asyncio.get_running_loop()
        loop_key = (id(loop), key)
        recheck: Callable[[], Awaitable[T | None]] | None = None
        while True:
            future, leader = self._claim_async(loop, loop_key)
            if leader:
                return await self._lead_async(loop_key, future, producer, recheck)
            try:
                value = copy.deepcopy(await asyncio.wait_for(asyncio.shield(future), self._timeout))
            except _LeaderAbandonedError:
                recheck = follower_loader
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
        self,
        key: str,
        future: Future[BaseModel],
        producer: Callable[[], T],
        recheck: Callable[[], T | None] | None,
    ) -> SingleFlightResult[T]:
        try:
            cached = recheck() if recheck is not None else None
            value = producer() if cached is None else cached
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
        return SingleFlightResult(value, cached is not None)

    async def _lead_async[T: BaseModel](
        self,
        loop_key: tuple[int, str],
        future: asyncio.Future[BaseModel],
        producer: Callable[[], Awaitable[T]],
        recheck: Callable[[], Awaitable[T | None]] | None,
    ) -> SingleFlightResult[T]:
        try:
            cached = await recheck() if recheck is not None else None
            value = await producer() if cached is None else cached
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
        return SingleFlightResult(value, cached is not None)

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
    """Coordinate leaders with Redis locks while followers load the shared cache result.

    A leader renews its lock lease every third of ``lock_ttl_seconds`` for as long
    as its producer runs, so the TTL only bounds how long a crashed leader blocks
    followers before one of them takes over.
    """

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
        """Lead under the Redis lock, or follow the shared cache and take over a freed lock."""

        lock_key = self._key(key)
        token = uuid.uuid4().hex
        client = self._connections.sync()
        if client.set(lock_key, token, nx=True, ex=self._lock_ttl):
            return self._lead(client, lock_key, token, producer)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if value := follower_loader():
                return SingleFlightResult(value, True)
            # A free lock with no cached result means the leader failed: take over.
            if client.set(lock_key, token, nx=True, ex=self._lock_ttl):
                return self._lead(client, lock_key, token, producer, follower_loader)
            time.sleep(self._poll)
        raise SingleFlightTimeoutError("Redis single-flight follower timed out")

    async def aexecute[T: BaseModel](
        self,
        key: str,
        producer: Callable[[], Awaitable[T]],
        follower_loader: Callable[[], Awaitable[T | None]],
    ) -> SingleFlightResult[T]:
        """Lead asynchronously under the Redis lock, or follow and take over a freed lock."""

        lock_key = self._key(key)
        token = uuid.uuid4().hex
        client = self._connections.async_client()
        if await client.set(lock_key, token, nx=True, ex=self._lock_ttl):
            return await self._alead(client, lock_key, token, producer)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if value := await follower_loader():
                return SingleFlightResult(value, True)
            if await client.set(lock_key, token, nx=True, ex=self._lock_ttl):
                return await self._alead(client, lock_key, token, producer, follower_loader)
            await asyncio.sleep(self._poll)
        raise SingleFlightTimeoutError("Redis single-flight follower timed out")

    def _lead[T: BaseModel](
        self,
        client: SyncRedisClient,
        lock_key: str,
        token: str,
        producer: Callable[[], T],
        recheck: Callable[[], T | None] | None = None,
    ) -> SingleFlightResult[T]:
        try:
            # A taking-over follower re-checks the cache: the leader may have stored
            # its result and released between the follower's miss and its SET NX.
            if recheck is not None and (cached := recheck()) is not None:
                return SingleFlightResult(cached, True)
            with self._renewing(client, lock_key, token):
                return SingleFlightResult(producer(), False)
        finally:
            client.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token)

    async def _alead[T: BaseModel](
        self,
        client: AsyncRedisClient,
        lock_key: str,
        token: str,
        producer: Callable[[], Awaitable[T]],
        recheck: Callable[[], Awaitable[T | None]] | None = None,
    ) -> SingleFlightResult[T]:
        try:
            if recheck is not None and (cached := await recheck()) is not None:
                return SingleFlightResult(cached, True)
            async with self._arenewing(client, lock_key, token):
                return SingleFlightResult(await producer(), False)
        finally:
            await client.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token)

    @contextlib.contextmanager
    def _renewing(self, client: SyncRedisClient, lock_key: str, token: str) -> Iterator[None]:
        """Renew the lock lease from a daemon thread and stop it before release."""

        stop = threading.Event()
        renewer = threading.Thread(
            target=self._renew_until_stopped,
            args=(client, lock_key, token, stop),
            name="harborrag-singleflight-renewer",
            daemon=True,
        )
        renewer.start()
        try:
            yield
        finally:
            stop.set()
            renewer.join(self._lock_ttl)

    def _renew_until_stopped(
        self, client: SyncRedisClient, lock_key: str, token: str, stop: threading.Event
    ) -> None:
        while not stop.wait(self._lock_ttl / 3):
            try:
                renewed = client.eval(
                    _RENEW_LOCK_SCRIPT, 1, lock_key, token, self._lock_ttl * 1_000
                )
            except Exception as exc:
                _LOGGER.warning("single-flight lock renewal failed: %s", type(exc).__name__)
                continue
            if not renewed:
                return

    @contextlib.asynccontextmanager
    async def _arenewing(
        self, client: AsyncRedisClient, lock_key: str, token: str
    ) -> AsyncIterator[None]:
        """Renew the lock lease from a task and stop it before release."""

        renewer = asyncio.create_task(self._arenew_until_cancelled(client, lock_key, token))
        try:
            yield
        finally:
            renewer.cancel()
            await asyncio.wait({renewer})

    async def _arenew_until_cancelled(
        self, client: AsyncRedisClient, lock_key: str, token: str
    ) -> None:
        while True:
            await asyncio.sleep(self._lock_ttl / 3)
            try:
                renewed = await client.eval(
                    _RENEW_LOCK_SCRIPT, 1, lock_key, token, self._lock_ttl * 1_000
                )
            except Exception as exc:
                _LOGGER.warning("single-flight lock renewal failed: %s", type(exc).__name__)
                continue
            if not renewed:
                return

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

_RENEW_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
