from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from harborrag_adapters.models.runtime.config import ConnectionPoolConfig
from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership

from .backend import ChatBackend
from .backends.direct import LiteLLMDirectBackend
from .backends.router import LiteLLMRouterBackend

type CompletionCallable = Callable[..., Any]
type AsyncCompletionCallable = Callable[..., Awaitable[Any]]
type ChatCompletionInvocation = ChatBackend


class LiteLLMChatInvocation(LiteLLMDirectBackend):
    """Backward-compatible name for the direct LiteLLM SDK chat backend."""

    def __init__(
        self,
        completion: CompletionCallable | None = None,
        acompletion: AsyncCompletionCallable | None = None,
        *,
        connections: SharedConnectionLifecycle | None = None,
    ) -> None:
        """Create a direct backend with an owned default connection lifecycle."""

        lifecycle = connections or SharedConnectionLifecycle(ConnectionPoolConfig(enabled=False))
        super().__init__(
            connections=lifecycle,
            connection_ownership=(
                ResourceOwnership.OWNED if connections is None else ResourceOwnership.BORROWED
            ),
            completion=completion,
            acompletion=acompletion,
        )


class LiteLLMChatRouterInvocation(LiteLLMRouterBackend):
    """Backward-compatible name for the in-process LiteLLM Router backend."""

    def __init__(
        self,
        router: Any,
        *,
        connections: SharedConnectionLifecycle | None = None,
    ) -> None:
        """Create a Router backend with an owned default connection lifecycle."""

        lifecycle = connections or SharedConnectionLifecycle(ConnectionPoolConfig(enabled=False))
        super().__init__(
            router,
            connections=lifecycle,
            connection_ownership=(
                ResourceOwnership.OWNED if connections is None else ResourceOwnership.BORROWED
            ),
        )
