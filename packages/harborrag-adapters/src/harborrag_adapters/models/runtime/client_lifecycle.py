from __future__ import annotations

import asyncio
import threading
from typing import ClassVar, Self

from .lifecycle import (
    AsyncLifecycleResource,
    LifecycleResource,
    close_async_resources,
    close_resources,
)
from .sync import AsyncLoopRunner


class ModelClientLifecycleMixin:
    """Provide context-manager and idempotent close behavior for model clients."""

    _closed: bool
    _sync_runner: AsyncLoopRunner | None = None
    _sync_runner_lock: ClassVar[threading.Lock] = threading.Lock()

    def __enter__(self) -> Self:
        """Enter the synchronous client lifecycle context."""

        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close all owned resources on synchronous context exit."""

        self.close()

    async def __aenter__(self) -> Self:
        """Enter the asynchronous client lifecycle context."""

        self._ensure_open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close all owned resources on asynchronous context exit."""

        await self.aclose()

    def sync_runner(self, *, thread_name: str) -> AsyncLoopRunner:
        """Return this client's one background loop for synchronous facades.

        Loop-bound resources such as the pooled connection session pin the first
        event loop they see, so every synchronous bridge over this client must
        share one loop. The runner is created lazily and stopped by ``close``.
        """

        with self._sync_runner_lock:
            self._ensure_open()
            if self._sync_runner is None:
                self._sync_runner = AsyncLoopRunner(thread_name=thread_name)
            return self._sync_runner

    def close(self) -> None:
        """Close all owned resources once and aggregate cleanup failures."""

        if self._closed:
            return
        self._closed = True
        runner = self._detach_sync_runner()
        if runner is None:
            close_resources(self._sync_resources())
            return
        # Resources bound to the runner loop must be released on that same loop.
        try:
            runner.run(close_async_resources(self._async_resources()))
        finally:
            runner.stop()

    async def aclose(self) -> None:
        """Asynchronously close all owned resources and aggregate failures."""

        if self._closed:
            return
        self._closed = True
        runner = self._detach_sync_runner()
        if runner is None:
            await close_async_resources(self._async_resources())
            return
        try:
            await asyncio.wrap_future(runner.submit(close_async_resources(self._async_resources())))
        finally:
            await asyncio.to_thread(runner.stop)

    def _detach_sync_runner(self) -> AsyncLoopRunner | None:
        with self._sync_runner_lock:
            runner, self._sync_runner = self._sync_runner, None
            return runner

    def _sync_resources(self) -> tuple[LifecycleResource, ...]:
        raise NotImplementedError

    def _async_resources(self) -> tuple[AsyncLifecycleResource, ...]:
        raise NotImplementedError

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"{type(self).__name__} is closed")
