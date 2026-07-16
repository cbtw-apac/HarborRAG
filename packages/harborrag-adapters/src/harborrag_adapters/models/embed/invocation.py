from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

EmbeddingCallable = Callable[..., Any]
AsyncEmbeddingCallable = Callable[..., Awaitable[Any]]


class EmbeddingInvocation(Protocol):
    """Define the injected sync/async embedding provider boundary."""

    def embed(self, **kwargs: Any) -> Any:
        """Invoke a synchronous embedding operation."""
        ...

    async def aembed(self, **kwargs: Any) -> Any:
        """Invoke an asynchronous embedding operation."""
        ...

    def close(self) -> None:
        """Release synchronous invocation resources."""
        ...

    async def aclose(self) -> None:
        """Release asynchronous invocation resources."""
        ...


class LiteLLMEmbeddingInvocation:
    """Call LiteLLM's provider-normalized embedding APIs."""

    def __init__(
        self,
        embedding: EmbeddingCallable | None = None,
        aembedding: AsyncEmbeddingCallable | None = None,
    ) -> None:
        """Store injected callables, defaulting to LiteLLM embedding functions."""

        if embedding is None or aembedding is None:
            import litellm

            embedding = embedding or litellm.embedding
            aembedding = aembedding or litellm.aembedding
        self._embedding = embedding
        self._aembedding = aembedding

    def embed(self, **kwargs: Any) -> Any:
        """Call LiteLLM's synchronous embedding API."""

        return self._embedding(**kwargs)

    async def aembed(self, **kwargs: Any) -> Any:
        """Call LiteLLM's asynchronous embedding API."""

        return await self._aembedding(**kwargs)

    def close(self) -> None:
        """Return without closing SDK-managed connection pools."""

        return None

    async def aclose(self) -> None:
        """Return without closing SDK-managed connection pools."""

        return None


class LiteLLMEmbeddingRouterInvocation(LiteLLMEmbeddingInvocation):
    """Expose a LiteLLM Router through the embedding invocation boundary."""

    def __init__(self, router: Any) -> None:
        self._router = router
        super().__init__(router.embedding, router.aembedding)

    def close(self) -> None:
        close = getattr(self._router, "close", None)
        if callable(close):
            close()

    async def aclose(self) -> None:
        aclose = getattr(self._router, "aclose", None)
        if callable(aclose):
            await aclose()
        else:
            self.close()
