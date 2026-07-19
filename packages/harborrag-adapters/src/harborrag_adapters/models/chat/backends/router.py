from __future__ import annotations

import inspect
from typing import Any

from harborrag_adapters.models.common.connections import SharedConnectionLifecycle
from harborrag_adapters.models.common.lifecycle import ResourceOwnership

from ..backend_config import ChatBackendType
from .base import BaseLiteLLMChatBackend


class LiteLLMRouterBackend(BaseLiteLLMChatBackend):
    """Invoke an in-process LiteLLM Router through the shared chat backend contract."""

    def __init__(
        self,
        router: Any,
        *,
        connections: SharedConnectionLifecycle,
        connection_ownership: ResourceOwnership = ResourceOwnership.BORROWED,
    ) -> None:
        """Bind Router completion functions and retain the Router for cleanup."""

        self._router = router
        super().__init__(
            ChatBackendType.LITELLM_ROUTER,
            router.completion,
            router.acompletion,
            connections=connections,
            connection_ownership=connection_ownership,
        )

    def close(self) -> None:
        """Flush and close the Router before releasing owned shared connections."""

        if self._closed:
            return
        self._flush()
        close = getattr(self._router, "close", None)
        if callable(close):
            close()
        super().close()

    async def aclose(self) -> None:
        """Asynchronously close the Router with a synchronous fallback."""

        if self._closed:
            return
        self._flush()
        aclose = getattr(self._router, "aclose", None)
        if callable(aclose):
            result = aclose()
            if inspect.isawaitable(result):
                await result
        else:
            close = getattr(self._router, "close", None)
            if callable(close):
                close()
        await super().aclose()

    def _flush(self) -> None:
        flush = getattr(self._router, "flush_cache", None)
        if callable(flush):
            flush()
