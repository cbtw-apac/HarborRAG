from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from harborrag_adapters.models.chat import HarborChatClientConfig
from harborrag_adapters.models.chat.configs import (
    HarborChatModelConfig,
    HarborChatProviderConfig,
)
from harborrag_adapters.models.chat.registry import HarborProvider
from harborrag_adapters.models.embed import (
    HarborEmbedClientConfig,
    HarborEmbedModelConfig,
    HarborEmbedProvider,
    HarborEmbedProviderConfig,
)
from harborrag_adapters.models.rerank import (
    HarborRerankClientConfig,
    HarborRerankModelConfig,
    HarborRerankProvider,
    HarborRerankProviderConfig,
)
from harborrag_adapters.models.runtime.config import (
    CacheConfig,
    ObservabilityConfig,
    RetryPolicyConfig,
    RoutingConfig,
    RoutingStrategy,
)
from harborrag_core.models.capabilities import (
    HarborChatCapabilities,
    HarborEmbedCapabilities,
    HarborRerankCapabilities,
)


def chat_config(
    *,
    deployments: tuple[HarborChatProviderConfig, ...] | None = None,
    retry: RetryPolicyConfig | None = None,
    routing: RoutingConfig | None = None,
    cache: CacheConfig | None = None,
    observability: ObservabilityConfig | None = None,
    fallbacks: tuple[str, ...] = (),
) -> HarborChatClientConfig:
    """Build a compact valid chat configuration for tests."""

    primary = deployments or (
        HarborChatProviderConfig(
            name="openai-a",
            provider=HarborProvider.OPENAI,
            model="openai/gpt-test",
            api_key="secret",
            capabilities=HarborChatCapabilities(
                structured_output=True,
                json_mode=True,
                tools=True,
                streaming=True,
            ),
        ),
    )
    models = {
        "primary": HarborChatModelConfig(deployments=primary, fallbacks=fallbacks),
    }
    if fallbacks:
        models["fallback"] = HarborChatModelConfig(
            deployments=(
                HarborChatProviderConfig(
                    name="fallback-a",
                    provider=HarborProvider.OPENAI,
                    model="openai/gpt-fallback",
                    api_key="secret",
                    capabilities=primary[0].capabilities,
                ),
            )
        )
    return HarborChatClientConfig(
        default_model="primary",
        models=models,
        retry=retry or RetryPolicyConfig(base_delay_seconds=0, max_delay_seconds=0),
        routing=routing or RoutingConfig(strategy=RoutingStrategy.ORDERED),
        cache=cache or CacheConfig(),
        observability=observability or ObservabilityConfig(enabled=False),
    )


def embed_config(
    *,
    deployments: tuple[HarborEmbedProviderConfig, ...] | None = None,
    retry: RetryPolicyConfig | None = None,
    cache: CacheConfig | None = None,
    fallbacks: tuple[str, ...] = (),
) -> HarborEmbedClientConfig:
    """Build a compact valid embedding configuration for tests."""

    primary = deployments or (
        HarborEmbedProviderConfig(
            name="embed-a",
            provider=HarborEmbedProvider.OPENAI,
            model="openai/text-embedding-test",
            api_key="secret",
            expected_dimensions=3,
            capabilities=HarborEmbedCapabilities(
                batch=True,
                configurable_dimensions=True,
                default_dimensions=3,
                encoding_format=True,
            ),
        ),
    )
    models = {
        "primary": HarborEmbedModelConfig(
            deployments=primary,
            embedding_space="test-space",
            fallbacks=fallbacks,
        )
    }
    if fallbacks:
        models["fallback"] = HarborEmbedModelConfig(
            deployments=(
                HarborEmbedProviderConfig(
                    name="embed-fallback",
                    provider=HarborEmbedProvider.OPENAI,
                    model="openai/text-embedding-fallback",
                    api_key="secret",
                    expected_dimensions=3,
                    capabilities=primary[0].capabilities,
                ),
            ),
            embedding_space="test-space",
        )
    return HarborEmbedClientConfig(
        default_model="primary",
        models=models,
        retry=retry or RetryPolicyConfig(base_delay_seconds=0, max_delay_seconds=0),
        routing=RoutingConfig(strategy=RoutingStrategy.ORDERED),
        cache=cache or CacheConfig(),
        observability=ObservabilityConfig(enabled=False),
        default_batch_size=2,
    )


def rerank_config(
    *,
    deployments: tuple[HarborRerankProviderConfig, ...] | None = None,
    retry: RetryPolicyConfig | None = None,
    cache: CacheConfig | None = None,
    fallbacks: tuple[str, ...] = (),
) -> HarborRerankClientConfig:
    """Build a compact valid reranking configuration for tests."""

    primary = deployments or (
        HarborRerankProviderConfig(
            name="rerank-a",
            provider=HarborRerankProvider.COHERE,
            model="cohere/rerank-test",
            api_key="secret",
            capabilities=HarborRerankCapabilities(),
        ),
    )
    models = {"primary": HarborRerankModelConfig(deployments=primary, fallbacks=fallbacks)}
    if fallbacks:
        models["fallback"] = HarborRerankModelConfig(
            deployments=(
                HarborRerankProviderConfig(
                    name="rerank-fallback",
                    provider=HarborRerankProvider.COHERE,
                    model="cohere/rerank-fallback",
                    api_key="secret",
                    capabilities=primary[0].capabilities,
                ),
            )
        )
    return HarborRerankClientConfig(
        default_model="primary",
        models=models,
        retry=retry or RetryPolicyConfig(base_delay_seconds=0, max_delay_seconds=0),
        routing=RoutingConfig(strategy=RoutingStrategy.ORDERED),
        cache=cache or CacheConfig(),
        observability=ObservabilityConfig(enabled=False),
    )


class FakeChatInvocation:
    """Return queued chat values or exceptions through every invocation boundary."""

    def __init__(self, values: list[Any]) -> None:
        """Store queued results and close-state observations."""

        self.values = list(values)
        self.calls: list[dict[str, Any]] = []
        self.closed = 0
        self.streams_closed = 0

    def _next(self, kwargs: dict[str, Any]) -> Any:
        self.calls.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def complete(self, **kwargs: Any) -> Any:
        """Return the next synchronous completion value."""

        return self._next(kwargs)

    async def acomplete(self, **kwargs: Any) -> Any:
        """Return the next asynchronous completion value."""

        return self._next(kwargs)

    def stream(self, **kwargs: Any) -> Iterator[Any]:
        """Return the next synchronous iterable or raise its queued exception."""

        value = self._next(kwargs)
        return iter(value)

    async def astream(self, **kwargs: Any) -> AsyncIterator[Any]:
        """Return the next asynchronous iterable or raise its queued exception."""

        value = self._next(kwargs)

        async def generator() -> AsyncIterator[Any]:
            for item in value:
                yield item

        return generator()

    def close_stream(self, stream: Any) -> None:
        """Record synchronous stream cleanup."""

        self.streams_closed += 1
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    async def aclose_stream(self, stream: Any) -> None:
        """Record asynchronous stream cleanup."""

        self.streams_closed += 1
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()

    def close(self) -> None:
        """Record synchronous invocation cleanup."""

        self.closed += 1

    async def aclose(self) -> None:
        """Record asynchronous invocation cleanup."""

        self.closed += 1


class FakeEmbeddingInvocation:
    """Return queued embedding values or exceptions."""

    def __init__(self, values: list[Any]) -> None:
        self.values = list(values)
        self.calls: list[dict[str, Any]] = []
        self.closed = 0

    def _next(self, kwargs: dict[str, Any]) -> Any:
        self.calls.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def embed(self, **kwargs: Any) -> Any:
        """Return the next synchronous embedding response."""

        return self._next(kwargs)

    async def aembed(self, **kwargs: Any) -> Any:
        """Return the next asynchronous embedding response."""

        return self._next(kwargs)

    def close(self) -> None:
        """Record synchronous invocation cleanup."""

        self.closed += 1

    async def aclose(self) -> None:
        """Record asynchronous invocation cleanup."""

        self.closed += 1


class FakeRerankInvocation(FakeEmbeddingInvocation):
    """Expose rerank method names over the queued provider fake."""

    def rerank(self, **kwargs: Any) -> Any:
        """Return the next synchronous reranking response."""

        return self._next(kwargs)

    async def arerank(self, **kwargs: Any) -> Any:
        """Return the next asynchronous reranking response."""

        return self._next(kwargs)
