from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from harborrag_adapters.models.common.connections import SharedConnectionLifecycle
from harborrag_adapters.models.common.lifecycle import ResourceOwnership
from harborrag_adapters.models.common.sync import run_awaitable_synchronously

from ..backend_config import ChatBackendType

type CompletionCallable = Callable[..., Any]
type AsyncCompletionCallable = Callable[..., Awaitable[Any]]


class BaseLiteLLMChatBackend:
    """Share stream cleanup and pooled async connection behavior across backends."""

    def __init__(
        self,
        backend_type: ChatBackendType,
        completion: CompletionCallable,
        acompletion: AsyncCompletionCallable,
        *,
        connections: SharedConnectionLifecycle,
        connection_ownership: ResourceOwnership,
    ) -> None:
        """Store callables and the explicitly owned or borrowed connection lifecycle."""

        self._backend_type = backend_type
        self._completion = completion
        self._acompletion = acompletion
        self._connections = connections
        self._owns_connections = connection_ownership is ResourceOwnership.OWNED
        self._closed = False

    @property
    def backend_type(self) -> ChatBackendType:
        """Return the concrete backend identity."""

        return self._backend_type

    def complete(self, **kwargs: Any) -> Any:
        """Invoke one synchronous completion after backend parameter preparation."""

        self._ensure_open()
        return self._completion(**self.prepare_parameters(kwargs))

    async def acomplete(self, **kwargs: Any) -> Any:
        """Invoke one async completion with the shared connection session."""

        self._ensure_open()
        params = self.prepare_parameters(kwargs)
        params.update(await self._connections.async_parameters())
        return await self._acompletion(**params)

    def stream(self, **kwargs: Any) -> Any:
        """Open one synchronous stream through the backend completion callable."""

        return self.complete(**kwargs)

    async def astream(self, **kwargs: Any) -> Any:
        """Open one asynchronous stream through the backend completion callable."""

        return await self.acomplete(**kwargs)

    def prepare_parameters(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Return backend-specific parameters without mutating caller data."""

        return dict(kwargs)

    def close_stream(self, stream: Any) -> None:
        """Close a synchronous stream, including async-only provider wrappers."""

        close = getattr(stream, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                run_awaitable_synchronously(result, thread_name="harbor-chat-stream-close")
            return
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            run_awaitable_synchronously(aclose(), thread_name="harbor-chat-stream-close")

    async def aclose_stream(self, stream: Any) -> None:
        """Close an asynchronous stream while accepting synchronous fallbacks."""

        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            await aclose()
            return
        close = getattr(stream, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def close(self) -> None:
        """Close owned shared connections once."""

        if self._closed:
            return
        self._closed = True
        if self._owns_connections:
            self._connections.close()

    async def aclose(self) -> None:
        """Asynchronously close owned shared connections once."""

        if self._closed:
            return
        self._closed = True
        if self._owns_connections:
            await self._connections.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("chat backend is closed")
