from __future__ import annotations

import pytest
from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_adapters.models.rerank import (
    HarborRerankClientConfig,
    HarborRerankingClient,
)
from model_invocation_support import (
    FakeEmbeddingInvocation,
    FakeRerankInvocation,
    embedding_response,
    rerank_response,
)

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
