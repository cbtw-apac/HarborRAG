from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from harborrag_adapters.models.runtime.litellm_import import require_litellm
from harborrag_core.models import errors as model_errors

RerankCallable = Callable[..., Any]
AsyncRerankCallable = Callable[..., Awaitable[Any]]


class RerankInvocation(Protocol):
    """Define a sync/async reranking boundary for LiteLLM or another supported adapter."""

    def rerank(self, **kwargs: Any) -> Any:
        """Invoke a synchronous reranking operation."""
        ...

    async def arerank(self, **kwargs: Any) -> Any:
        """Invoke an asynchronous reranking operation."""
        ...

    def close(self) -> None:
        """Release synchronous invocation resources."""
        ...

    async def aclose(self) -> None:
        """Release asynchronous invocation resources."""
        ...


class LiteLLMRerankInvocation:
    """Call LiteLLM's provider-normalized reranking APIs."""

    def __init__(
        self,
        rerank: RerankCallable | None = None,
        arerank: AsyncRerankCallable | None = None,
    ) -> None:
        """Store injected callables, defaulting to LiteLLM reranking functions."""

        if rerank is None or arerank is None:
            litellm = require_litellm(model_errors.HarborRerankConfigurationError)

            rerank = rerank or litellm.rerank
            arerank = arerank or litellm.arerank
        self._rerank = rerank
        self._arerank = arerank

    def rerank(self, **kwargs: Any) -> Any:
        """Call LiteLLM's synchronous reranking API."""

        return self._rerank(**kwargs)

    async def arerank(self, **kwargs: Any) -> Any:
        """Call LiteLLM's asynchronous reranking API."""

        return await self._arerank(**kwargs)

    def close(self) -> None:
        """Return without closing SDK-managed connection pools."""

        return None

    async def aclose(self) -> None:
        """Return without closing SDK-managed connection pools."""

        return None
