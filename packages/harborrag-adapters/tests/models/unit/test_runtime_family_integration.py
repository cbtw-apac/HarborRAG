from __future__ import annotations

import pytest
from model_invocation_support import (
    FakeEmbeddingInvocation,
    FakeRerankInvocation,
    embedding_response,
    rerank_response,
)

from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_adapters.models.rerank import (
    HarborRerankClientConfig,
    HarborRerankingClient,
)
from harborrag_adapters.models.runtime.cache import InMemoryModelCache

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_embedding_deployment_failover_and_cache() -> None:
    config = HarborEmbedClientConfig.from_dict(
        {
            "default_model": "embed",
            "retry": {
                "same_deployment_attempts": 1,
                "max_deployment_failovers": 1,
                "base_delay_seconds": 0,
                "max_delay_seconds": 0,
            },
            "routing": {"strategy": "ordered"},
            "cache": {"enabled": True},
            "models": {
                "embed": {
                    "embedding_space": "shared",
                    "deployments": [
                        {
                            "name": "embed-a",
                            "provider": "openai",
                            "model": "openai/a",
                            "api_key": "key",
                            "expected_dimensions": 2,
                            "order": 0,
                        },
                        {
                            "name": "embed-b",
                            "provider": "openai",
                            "model": "openai/b",
                            "api_key": "key",
                            "expected_dimensions": 2,
                            "order": 1,
                        },
                    ],
                }
            },
        }
    )
    invocation = FakeEmbeddingInvocation([TimeoutError("slow"), embedding_response([[1.0, 0.0]])])
    client = HarborEmbedClient(config, invocation=invocation)

    first = client.embed("hello", metadata={"tenant_id": "tenant"})
    cached = client.embed("hello", metadata={"tenant_id": "tenant"})

    assert first.deployment == "embed-b"
    assert first.provider_metadata["routing"]["deployment_failovers"] == 1
    assert cached.cache_hit is True
    assert len(invocation.calls) == 2


def test_rerank_logical_fallback_and_cache() -> None:
    config = HarborRerankClientConfig.from_dict(
        {
            "default_model": "primary",
            "retry": {
                "same_deployment_attempts": 1,
                "max_model_fallbacks": 1,
                "base_delay_seconds": 0,
                "max_delay_seconds": 0,
            },
            "cache": {"enabled": True},
            "models": {
                "primary": {
                    "fallbacks": ["secondary"],
                    "provider": "cohere",
                    "model": "cohere/primary",
                    "api_key": "key",
                },
                "secondary": {
                    "provider": "cohere",
                    "model": "cohere/secondary",
                    "api_key": "key",
                },
            },
        }
    )
    invocation = FakeRerankInvocation([TimeoutError("slow"), rerank_response([(0, 0.9)])])
    client = HarborRerankingClient(config, invocation=invocation)

    first = client.rerank("query", ["document"], metadata={"tenant_id": "tenant"})
    cached = client.rerank("query", ["document"], metadata={"tenant_id": "tenant"})

    assert first.logical_model == "secondary"
    assert first.provider_metadata["routing"]["model_fallbacks"] == 1
    assert cached.cache_hit is True
    assert len(invocation.calls) == 2


def _embed_config(model: str) -> HarborEmbedClientConfig:
    return HarborEmbedClientConfig.from_dict(
        {
            "default_model": "embed",
            "cache": {"enabled": True},
            "models": {
                "embed": {
                    "provider": "openai",
                    "model": model,
                    "api_key": "key",
                    "expected_dimensions": 2,
                }
            },
        }
    )


def _rerank_config(model: str) -> HarborRerankClientConfig:
    return HarborRerankClientConfig.from_dict(
        {
            "default_model": "rerank",
            "cache": {"enabled": True},
            "models": {"rerank": {"provider": "cohere", "model": model, "api_key": "key"}},
        }
    )


def test_embedding_cache_is_partitioned_by_the_model_configuration() -> None:
    """Repointing a logical model must not serve the old model's vectors from a shared cache."""
    shared_cache = InMemoryModelCache(max_entries=8)
    old = HarborEmbedClient(
        _embed_config("openai/a"),
        invocation=FakeEmbeddingInvocation([embedding_response([[1.0, 0.0]])]),
        cache=shared_cache,
    )
    new_invocation = FakeEmbeddingInvocation([embedding_response([[0.0, 1.0]])])
    repointed = HarborEmbedClient(
        _embed_config("openai/b"), invocation=new_invocation, cache=shared_cache
    )

    old.embed("hello", metadata={"tenant_id": "tenant"})
    response = repointed.embed("hello", metadata={"tenant_id": "tenant"})

    assert response.cache_hit is False
    assert response.vectors == ((0.0, 1.0),)
    assert len(new_invocation.calls) == 1


def test_rerank_cache_is_partitioned_by_the_model_configuration() -> None:
    shared_cache = InMemoryModelCache(max_entries=8)
    old = HarborRerankingClient(
        _rerank_config("cohere/a"),
        invocation=FakeRerankInvocation([rerank_response([(0, 0.9)])]),
        cache=shared_cache,
    )
    new_invocation = FakeRerankInvocation([rerank_response([(0, 0.1)])])
    repointed = HarborRerankingClient(
        _rerank_config("cohere/b"), invocation=new_invocation, cache=shared_cache
    )

    old.rerank("query", ["document"], metadata={"tenant_id": "tenant"})
    response = repointed.rerank("query", ["document"], metadata={"tenant_id": "tenant"})

    assert response.cache_hit is False
    assert len(new_invocation.calls) == 1
