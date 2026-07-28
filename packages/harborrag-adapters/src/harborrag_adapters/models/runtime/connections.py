from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .config import ConnectionPoolConfig
from .lifecycle import ResourceOwnership
from .sync import run_awaitable_synchronously

type AsyncSessionFactory = Callable[[ConnectionPoolConfig], Any]


class SharedConnectionLifecycle:
    """Own or borrow one lazily created async HTTP session for LiteLLM calls."""

    def __init__(
        self,
        config: ConnectionPoolConfig,
        *,
        async_session: Any | None = None,
        ownership: ResourceOwnership = ResourceOwnership.BORROWED,
        session_factory: AsyncSessionFactory | None = None,
    ) -> None:
        """Store pooling settings without creating event-loop-bound resources eagerly."""

        self.config = config
        self._async_session = async_session
        self._ownership = ownership
        self._factory = session_factory or _default_session_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        """Return whether this lifecycle has released its owned resources."""

        return self._closed

    async def async_parameters(self) -> dict[str, Any]:
        """Return LiteLLM parameters containing the shared async session when enabled."""

        if not self.config.enabled:
            return {}
        session = await self.async_session()
        return {"shared_session": session}

    async def async_session(self) -> Any:
        """Create and return one session bound to the caller's active event loop."""

        self._ensure_open()
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("shared connection lifecycle cannot span multiple event loops")
        if self._async_session is not None:
            self._loop = self._loop or loop
            return self._async_session
        self._loop = loop
        self._lock = self._lock or asyncio.Lock()
        async with self._lock:
            if self._async_session is None:
                self._async_session = self._factory(self.config)
                self._ownership = ResourceOwnership.OWNED
        return self._async_session

    def close(self) -> None:
        """Release the owned async session from synchronous application shutdown."""

        if self._closed:
            return
        self._closed = True
        if self._ownership is ResourceOwnership.BORROWED or self._async_session is None:
            return
        close = getattr(self._async_session, "close", None)
        if not callable(close):
            return
        result = close()
        if isinstance(result, Awaitable):
            run_awaitable_synchronously(result, thread_name="harbor-model-connection-close")

    async def aclose(self) -> None:
        """Release the owned async session during asynchronous application shutdown."""

        if self._closed:
            return
        self._closed = True
        if self._ownership is ResourceOwnership.BORROWED or self._async_session is None:
            return
        close = getattr(self._async_session, "close", None)
        if not callable(close):
            return
        result = close()
        if isinstance(result, Awaitable):
            await result

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("shared connection lifecycle is closed")


def _default_session_factory(config: ConnectionPoolConfig) -> Any:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=config.total_timeout_seconds)
    connector = aiohttp.TCPConnector(
        limit=config.connection_limit,
        limit_per_host=config.connection_limit_per_host,
        ttl_dns_cache=config.dns_cache_seconds,
        keepalive_timeout=config.keepalive_seconds,
        enable_cleanup_closed=True,
    )
    return aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=config.trust_env)
