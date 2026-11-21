from __future__ import annotations

import litellm
import pytest
from model_invocation_support import (
    FakeEmbeddingInvocation,
    FakeRerankInvocation,
    embedding_response,
    rerank_response,
)
from model_runtime_support import embed_config, rerank_config

from harborrag_adapters.models.embed import (
    HarborEmbedClient,
    HarborEmbedProvider,
    HarborEmbedProviderConfig,
)
from harborrag_adapters.models.rerank import (
    HarborRerankingClient,
    HarborRerankProvider,
    HarborRerankProviderConfig,
)
from harborrag_adapters.models.runtime.cache import InMemoryModelCache
from harborrag_adapters.models.runtime.config import CacheConfig
from harborrag_core.models.capabilities import HarborEmbedCapabilities, HarborRerankCapabilities
from harborrag_core.models.errors import HarborEmbedError

pytestmark = pytest.mark.unit

_TENANT = {"tenant_id": "tenant-a"}


def _bad_request() -> Exception:
    return litellm.BadRequestError(message="refused", model="m", llm_provider="openai")


def _assert_finite_cause_chain(error: BaseException) -> None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None:
        assert id(current) not in seen, "the cause chain loops back on itself"
        seen.add(id(current))
        current = current.__cause__


def test_permanent_embed_failure_keeps_the_provider_error_as_its_cause() -> None:
    raw = _bad_request()
    client = HarborEmbedClient(embed_config(), invocation=FakeEmbeddingInvocation([raw]))

    with pytest.raises(HarborEmbedError) as caught:
        client.embed("synthetic text")

    assert caught.value.__cause__ is raw
    _assert_finite_cause_chain(caught.value)


@pytest.mark.asyncio
async def test_async_permanent_embed_failure_keeps_the_provider_error_as_its_cause() -> None:
    raw = _bad_request()
    client = HarborEmbedClient(embed_config(), invocation=FakeEmbeddingInvocation([raw]))

    with pytest.raises(HarborEmbedError) as caught:
        await client.aembed("synthetic text")

    assert caught.value.__cause__ is raw
    _assert_finite_cause_chain(caught.value)


def _embed_deployment(model: str) -> HarborEmbedProviderConfig:
    return HarborEmbedProviderConfig(
        name="embed-a",
        provider=HarborEmbedProvider.OPENAI,
        model=model,
        api_key="synthetic-key",
        expected_dimensions=3,
        capabilities=HarborEmbedCapabilities(batch=True, default_dimensions=3),
    )


def test_embed_cache_is_not_shared_across_catalog_changes() -> None:
    shared = InMemoryModelCache(max_entries=16)
    cache = CacheConfig(enabled=True)
    before = FakeEmbeddingInvocation([embedding_response([[1.0, 0.0, 0.0]])])
    after = FakeEmbeddingInvocation([embedding_response([[0.0, 1.0, 0.0]])])
    old = embed_config(deployments=(_embed_deployment("openai/embed-old"),), cache=cache)
    new = embed_config(deployments=(_embed_deployment("openai/embed-new"),), cache=cache)

    HarborEmbedClient(old, invocation=before, cache=shared).embed("hello", metadata=_TENANT)
    response = HarborEmbedClient(new, invocation=after, cache=shared).embed(
        "hello", metadata=_TENANT
    )

    assert response.cache_hit is False
    assert len(after.calls) == 1


def _rerank_deployment(model: str) -> HarborRerankProviderConfig:
    return HarborRerankProviderConfig(
        name="rerank-a",
        provider=HarborRerankProvider.COHERE,
        model=model,
        api_key="synthetic-key",
        capabilities=HarborRerankCapabilities(),
    )


def test_rerank_cache_is_not_shared_across_catalog_changes() -> None:
    shared = InMemoryModelCache(max_entries=16)
    cache = CacheConfig(enabled=True)
    before = FakeRerankInvocation([rerank_response([(0, 0.9)])])
    after = FakeRerankInvocation([rerank_response([(0, 0.2)])])
    old = rerank_config(deployments=(_rerank_deployment("cohere/rerank-old"),), cache=cache)
    new = rerank_config(deployments=(_rerank_deployment("cohere/rerank-new"),), cache=cache)

    HarborRerankingClient(old, invocation=before, cache=shared).rerank(
        "question", ["passage"], metadata=_TENANT
    )
    response = HarborRerankingClient(new, invocation=after, cache=shared).rerank(
        "question", ["passage"], metadata=_TENANT
    )

    assert response.cache_hit is False
    assert len(after.calls) == 1
