from __future__ import annotations

from typing import Self

from .lifecycle import (
    AsyncLifecycleResource,
    LifecycleResource,
    close_async_resources,
    close_resources,
)


class ModelClientLifecycleMixin:
    """Provide context-manager and idempotent close behavior for model clients."""

    _closed: bool

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

    def close(self) -> None:
        """Close all owned resources once and aggregate cleanup failures."""

        if self._closed:
            return
        self._closed = True
        close_resources(self._sync_resources())

    async def aclose(self) -> None:
        """Asynchronously close all owned resources and aggregate failures."""

        if self._closed:
            return
        self._closed = True
        await close_async_resources(self._async_resources())

    def _sync_resources(self) -> tuple[LifecycleResource, ...]:
        raise NotImplementedError

    def _async_resources(self) -> tuple[AsyncLifecycleResource, ...]:
        raise NotImplementedError

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"{type(self).__name__} is closed")
