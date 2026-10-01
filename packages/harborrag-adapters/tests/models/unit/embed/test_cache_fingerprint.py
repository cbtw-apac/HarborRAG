from __future__ import annotations

import pytest
from model_runtime_support import FakeEmbeddingInvocation, embed_config

from harborrag_adapters.models.embed import HarborEmbedClient
from harborrag_adapters.models.embed.configs import HarborEmbedProviderConfig
from harborrag_adapters.models.embed.registry import HarborEmbedProvider
from harborrag_adapters.models.runtime.cache import InMemoryModelCache
from harborrag_adapters.models.runtime.config import CacheConfig
from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.embed import HarborEmbedRequest

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _deployment(*, model: str) -> HarborEmbedProviderConfig:
    """Build one embedding deployment pinned to the given provider model."""
    return HarborEmbedProviderConfig(
        name="embed-a",
        provider=HarborEmbedProvider.OPENAI,
        model=model,
        api_key="secret",
        expected_dimensions=3,
        capabilities=HarborEmbedCapabilities(
            batch=True,
            configurable_dimensions=True,
            default_dimensions=3,
            encoding_format=True,
        ),
    )


def _raw_batch(*vectors: list[float]) -> dict[str, object]:
    """Build one LiteLLM-style embedding response batch."""
    return {
        "model": "provider-embed",
        "data": [{"index": index, "embedding": value} for index, value in enumerate(vectors)],
        "usage": {"prompt_tokens": len(vectors), "total_tokens": len(vectors)},
    }


def test_repointed_embedding_model_does_not_serve_previous_models_cached_vectors() -> None:
    """A deployment's provider model change must partition the shared response cache.

    Two configurations that differ only in a deployment's provider model must not
    collide on the same cache key: otherwise a repointed embedding model keeps
    serving the previous model's vectors from a shared cache.
    """
    cache_config = CacheConfig(enabled=True, ttl_seconds=30)
    request = HarborEmbedRequest(inputs=("hello",), metadata={"tenant_id": "tenant"}, cacheable=True)

    model_a = HarborEmbedClient(
        embed_config(deployments=(_deployment(model="openai/model-a"),), cache=cache_config),
        invocation=FakeEmbeddingInvocation([_raw_batch([1, 0, 0])]),
    )
    model_b = HarborEmbedClient(
        embed_config(deployments=(_deployment(model="openai/model-b"),), cache=cache_config),
        invocation=FakeEmbeddingInvocation([_raw_batch([0, 1, 0])]),
    )
    key_a = model_a._execution.cache.decision(request, "primary").key
    key_b = model_b._execution.cache.decision(request, "primary").key
    assert key_a is not None
    assert key_b is not None
    assert key_a != key_b

    shared_cache = InMemoryModelCache()
    first_invocation = FakeEmbeddingInvocation([_raw_batch([1, 0, 0])])
    first_client = HarborEmbedClient(
        embed_config(deployments=(_deployment(model="openai/model-a"),), cache=cache_config),
        invocation=first_invocation,
        cache=shared_cache,
    )
    assert first_client._execution.cache.decision(request, "primary").key == key_a
    first = first_client.embed(request=request)
    assert first.embeddings[0].value == (1.0, 0.0, 0.0)

    second_invocation = FakeEmbeddingInvocation([_raw_batch([0, 1, 0])])
    second_client = HarborEmbedClient(
        embed_config(deployments=(_deployment(model="openai/model-b"),), cache=cache_config),
        invocation=second_invocation,
        cache=shared_cache,
    )
    second = second_client.embed(request=request)
    assert second.cache_hit is False
    assert second.embeddings[0].value == (0.0, 1.0, 0.0)
    assert len(second_invocation.calls) == 1
