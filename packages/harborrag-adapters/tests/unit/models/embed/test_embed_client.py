from __future__ import annotations

import math

import pytest
from harborrag_adapters.models.common.lifecycle import ResourceOwnership
from harborrag_adapters.models.embed import (
    HarborEmbedClient,
    HarborEmbedClientConfig,
)
from harborrag_core.models.errors import (
    HarborEmbedCapabilityError,
    HarborEmbedInvalidRequestError,
    HarborEmbedMalformedResponseError,
    HarborEmbedPartialBatchError,
    HarborEmbedProviderError,
    HarborEmbedTimeoutError,
)
from model_invocation_support import FakeEmbeddingInvocation, embedding_response


def embed_config(
    *,
    batch_size: int = 2,
    attempts: int = 1,
    capabilities: dict[str, object] | None = None,
) -> HarborEmbedClientConfig:
    return HarborEmbedClientConfig.from_dict(
        {
            "default_model": "primary",
            "default_batch_size": batch_size,
            "retry": {
                "same_deployment_attempts": attempts,
                "base_delay_seconds": 0,
                "max_delay_seconds": 0,
            },
            "timeouts": {"request_seconds": 11},
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/text-embedding-3-small",
                    "api_key": "key",
                    "expected_dimensions": 2,
                    "capabilities": capabilities or {},
                }
            },
        }
    )


def test_sync_single_input_embedding() -> None:
    invocation = FakeEmbeddingInvocation([embedding_response([[1.0, 2.0]], prompt_tokens=3)])
    client = HarborEmbedClient(embed_config(), invocation=invocation)

    response = client.embed("hello")

    assert response.vectors == ((1.0, 2.0),)
    assert response.usage.prompt_tokens == 3
    assert response.logical_model == "primary"
    assert invocation.calls[0]["input"] == ["hello"]
    assert invocation.calls[0]["timeout"] == 11
    assert "encoding_format" not in invocation.calls[0]


@pytest.mark.asyncio
async def test_async_batch_embedding() -> None:
    invocation = FakeEmbeddingInvocation([embedding_response([[1.0, 0.0], [0.0, 1.0]])])
    client = HarborEmbedClient(embed_config(), invocation=invocation)

    response = await client.aembed(["first", "second"])

    assert response.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert invocation.async_calls[0]["input"] == ["first", "second"]


def test_configurable_batching_and_usage_aggregation() -> None:
    invocation = FakeEmbeddingInvocation(
        [
            embedding_response([[1.0, 0.0], [2.0, 0.0]], prompt_tokens=4),
            embedding_response([[3.0, 0.0]], prompt_tokens=2),
        ]
    )

    response = HarborEmbedClient(embed_config(), invocation=invocation).embed(
        ["a", "b", "c"], batch_size=2
    )

    assert [call["input"] for call in invocation.calls] == [["a", "b"], ["c"]]
    assert response.usage.prompt_tokens == 6
    assert response.usage.total_tokens == 6


def test_provider_indexes_are_restored_to_original_input_order() -> None:
    invocation = FakeEmbeddingInvocation(
        [embedding_response([[2.0, 0.0], [1.0, 0.0]], indexes=[1, 0])]
    )

    response = HarborEmbedClient(embed_config(), invocation=invocation).embed(["a", "b"])

    assert response.vectors == ((1.0, 0.0), (2.0, 0.0))
    assert tuple(item.index for item in response.embeddings) == (0, 1)


def test_dimension_mismatch_is_explicit() -> None:
    client = HarborEmbedClient(
        embed_config(),
        invocation=FakeEmbeddingInvocation([embedding_response([[1.0, 2.0, 3.0]])]),
    )

    with pytest.raises(HarborEmbedMalformedResponseError, match="expected 2 dimensions"):
        client.embed("hello")


def test_empty_input_is_rejected_before_invocation() -> None:
    invocation = FakeEmbeddingInvocation()

    with pytest.raises(HarborEmbedInvalidRequestError, match="invalid embedding request"):
        HarborEmbedClient(embed_config(), invocation=invocation).embed([])

    assert invocation.calls == []


def test_partial_batch_failure_never_returns_partial_vectors() -> None:
    invocation = FakeEmbeddingInvocation(
        [embedding_response([[1.0, 0.0], [2.0, 0.0]]), RuntimeError("provider failed")]
    )

    with pytest.raises(HarborEmbedPartialBatchError) as captured:
        HarborEmbedClient(embed_config(), invocation=invocation).embed(["a", "b", "c"])

    assert captured.value.metadata == {
        "failed_batch_index": 1,
        "completed_inputs": 2,
        "total_inputs": 3,
    }


def test_provider_error_is_normalized() -> None:
    client = HarborEmbedClient(
        embed_config(),
        invocation=FakeEmbeddingInvocation([RuntimeError("provider failed")]),
    )

    with pytest.raises(HarborEmbedProviderError) as captured:
        client.embed("hello")

    assert captured.value.provider == "openai"
    assert captured.value.request_id


def test_timeout_is_retried_and_remains_typed_when_exhausted() -> None:
    invocation = FakeEmbeddingInvocation([TimeoutError("slow"), TimeoutError("still slow")])

    with pytest.raises(HarborEmbedTimeoutError):
        HarborEmbedClient(embed_config(attempts=2), invocation=invocation).embed("hello")

    assert len(invocation.calls) == 2


def test_optional_vector_normalization() -> None:
    invocation = FakeEmbeddingInvocation([embedding_response([[3.0, 4.0]])])

    response = HarborEmbedClient(embed_config(), invocation=invocation).embed(
        "hello", normalize=True
    )

    assert response.normalized is True
    assert response.vectors[0] == pytest.approx((0.6, 0.8))
    assert math.sqrt(sum(value * value for value in response.vectors[0])) == pytest.approx(1.0)


def test_token_inputs_require_declared_capability() -> None:
    invocation = FakeEmbeddingInvocation()

    with pytest.raises(HarborEmbedCapabilityError, match="token-array"):
        HarborEmbedClient(embed_config(), invocation=invocation).embed([1, 2, 3])

    assert invocation.calls == []


@pytest.mark.asyncio
async def test_lifecycle_closes_owned_invocation_once() -> None:
    sync_invocation = FakeEmbeddingInvocation()
    sync_client = HarborEmbedClient(embed_config(), invocation=sync_invocation)
    sync_client.close()
    sync_client.close()
    assert sync_invocation.close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        sync_client.embed("hello")

    async_invocation = FakeEmbeddingInvocation()
    async_client = HarborEmbedClient(embed_config(), invocation=async_invocation)
    await async_client.aclose()
    await async_client.aclose()
    assert async_invocation.aclose_count == 1


def test_borrowed_invocation_is_not_closed() -> None:
    invocation = FakeEmbeddingInvocation()
    client = HarborEmbedClient(
        embed_config(),
        invocation=invocation,
        resource_ownership=ResourceOwnership.BORROWED,
    )

    client.close()

    assert invocation.close_count == 0
