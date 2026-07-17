from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from harborrag_adapters.models.common.connections import SharedConnectionLifecycle
from harborrag_adapters.models.common.lifecycle import ResourceOwnership

from ..backend_config import ChatBackendType
from .base import BaseLiteLLMChatBackend

type CompletionCallable = Callable[..., Any]
type AsyncCompletionCallable = Callable[..., Awaitable[Any]]


class LiteLLMDirectBackend(BaseLiteLLMChatBackend):
    """Call provider models directly through LiteLLM's Python SDK functions."""

    def __init__(
        self,
        *,
        connections: SharedConnectionLifecycle,
        connection_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
        completion: CompletionCallable | None = None,
        acompletion: AsyncCompletionCallable | None = None,
    ) -> None:
        """Bind injected functions or current LiteLLM completion entrypoints."""

        if completion is None or acompletion is None:
            import litellm

            completion = completion or litellm.completion
            acompletion = acompletion or litellm.acompletion
        super().__init__(
            ChatBackendType.DIRECT_SDK,
            completion,
            acompletion,
            connections=connections,
            connection_ownership=connection_ownership,
        )
