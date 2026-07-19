from __future__ import annotations

from typing import Any, Protocol

from .backend_config import ChatBackendType


class ChatBackend(Protocol):
    """Define the transport contract implemented by every chat backend."""

    @property
    def backend_type(self) -> ChatBackendType:
        """Return the configured backend identity."""

        ...

    def complete(self, **kwargs: Any) -> Any:
        """Invoke one synchronous chat completion."""

        ...

    async def acomplete(self, **kwargs: Any) -> Any:
        """Invoke one asynchronous chat completion."""

        ...

    def stream(self, **kwargs: Any) -> Any:
        """Open one synchronous chat stream."""

        ...

    async def astream(self, **kwargs: Any) -> Any:
        """Open one asynchronous chat stream."""

        ...

    def close_stream(self, stream: Any) -> None:
        """Release one synchronous stream resource."""

        ...

    async def aclose_stream(self, stream: Any) -> None:
        """Release one asynchronous stream resource."""

        ...

    def close(self) -> None:
        """Release synchronous backend resources."""

        ...

    async def aclose(self) -> None:
        """Release asynchronous backend resources."""

        ...
