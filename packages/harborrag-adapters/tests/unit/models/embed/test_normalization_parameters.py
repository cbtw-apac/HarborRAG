from __future__ import annotations

import math
from typing import Any

import pytest
from harborrag_adapters.models.embed.batching import EmbeddingBatchAccumulator
from harborrag_adapters.models.embed.configs import (
    HarborEmbedDefaults,
    HarborEmbedProviderConfig,
)
from harborrag_adapters.models.embed.normalization import (
    merge_embedding_batches,
    normalize_embedding_batch,
)
from harborrag_adapters.models.embed.parameters import (
    apply_embed_defaults,
    build_embed_request,
    build_litellm_parameters,
    effective_batch_size,
    ensure_embed_request_id,
    litellm_inputs,
    normalize_embedding_inputs,
    prepare_embed_request,
)
from harborrag_adapters.models.embed.registry import HarborEmbedProvider
from harborrag_adapters.models.embed.validation import validate_embed_request
from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.embed import (
    EmbeddingEncodingFormat,
    EmbeddingPurpose,
    HarborEmbedMetadata,
    HarborEmbedRequest,
)
from harborrag_core.models.errors import (
    HarborEmbedCapabilityError,
    HarborEmbedConfigurationError,
    HarborEmbedInvalidRequestError,
    HarborEmbedMalformedResponseError,
    HarborEmbedPartialBatchError,
    HarborEmbedProviderError,
)
from model_runtime_support import embed_config
from pydantic import SecretStr

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def deployment(**updates: Any) -> HarborEmbedProviderConfig:
    values: dict[str, Any] = {
        "name": "embed-a",
        "provider": HarborEmbedProvider.OPENAI,
        "model": "openai/embed-test",
        "api_key": "secret",
        "headers": {"X-Deploy": SecretStr("one")},
        "expected_dimensions": 3,
        "max_batch_size": 4,
        "capabilities": HarborEmbedCapabilities(
            batch=True,
            max_batch_size=3,
            token_inputs=True,
            configurable_dimensions=True,
            default_dimensions=3,
            encoding_format=True,
            purpose=True,
            supported_purposes=frozenset(EmbeddingPurpose),
        ),
    }
    values.update(updates)
    return HarborEmbedProviderConfig(**values)


def raw_batch(*vectors: list[float], model: str = "provider-embed") -> dict[str, Any]:
    return {
        "model": model,
        "data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)],
        "usage": {"prompt_tokens": len(vectors), "total_tokens": len(vectors) + 1},
        "_hidden_params": {
            "custom_llm_provider": "openai",
            "model_id": "embed-a",
            "request_id": "provider-request",
            "response_cost": 0.02,
            "cache_hit": True,
            "region_name": "us",
        },
    }


def test_normalize_embedding_batch_restores_indexes_and_metadata() -> None:
    response = normalize_embedding_batch(
        raw_batch([3, 4, 0], [0, 1, 0]),
        input_count=2,
        index_offset=5,
        logical_model="primary",
        embedding_space="space",
        deployment=deployment(),
        request_id="req",
        latency_ms=2,
        normalize_vectors=True,
    )
    assert [item.index for item in response.embeddings] == [5, 6]
    assert math.isclose(response.embeddings[0].value[0], 0.6)
    assert response.dimensions == 3
    assert response.usage.total_tokens == 3
    assert response.provider_request_id == "provider-request"
    assert response.estimated_cost_usd == 0.02
    assert response.cache_hit


@pytest.mark.parametrize(
    "raw,input_count",
    [
        ({}, 1),
        ({"data": []}, 1),
        ({"data": [{"index": -1, "embedding": [1, 2, 3]}]}, 1),
        (
            {
                "data": [
                    {"index": 0, "embedding": [1, 2, 3]},
                    {"index": 0, "embedding": [1, 2, 3]},
                ]
            },
            2,
        ),
        ({"data": [{"embedding": []}]}, 1),
        ({"data": [{"embedding": [0, 0, 0]}]}, 1),
        ({"data": [{"embedding": [1, float("inf"), 3]}]}, 1),
        ({"data": [{"embedding": ["bad", 2, 3]}]}, 1),
        ({"data": [{"embedding": {"x": 1}}]}, 1),
    ],
)
def test_malformed_embedding_batches_are_rejected(raw: object, input_count: int) -> None:
    with pytest.raises(HarborEmbedMalformedResponseError):
        normalize_embedding_batch(
            raw,
            input_count=input_count,
            index_offset=0,
            logical_model="primary",
            embedding_space="space",
            deployment=deployment(),
            request_id="req",
            latency_ms=1,
            normalize_vectors=True,
        )


def test_base64_embedding_rules_and_dimension_mismatch() -> None:
    encoded = normalize_embedding_batch(
        {"data": [{"embedding": "encoded"}]},
        input_count=1,
        index_offset=0,
        logical_model="primary",
        embedding_space="space",
        deployment=deployment(),
        request_id="req",
        latency_ms=1,
        normalize_vectors=False,
    )
    assert encoded.embeddings[0].value == "encoded"
    with pytest.raises(HarborEmbedMalformedResponseError):
        normalize_embedding_batch(
            {"data": [{"embedding": "encoded"}]},
            input_count=1,
            index_offset=0,
            logical_model="primary",
            embedding_space="space",
            deployment=deployment(),
            request_id="req",
            latency_ms=1,
            normalize_vectors=True,
        )
    with pytest.raises(HarborEmbedMalformedResponseError, match="expected 3"):
        normalize_embedding_batch(
            {"data": [{"embedding": [1, 2]}]},
            input_count=1,
            index_offset=0,
            logical_model="primary",
            embedding_space="space",
            deployment=deployment(),
            request_id="req",
            latency_ms=1,
            normalize_vectors=False,
        )


def test_merge_embedding_batches_combines_usage_cost_and_order() -> None:
    first = normalize_embedding_batch(
        raw_batch([1, 0, 0]),
        input_count=1,
        index_offset=1,
        logical_model="primary",
        embedding_space="space",
        deployment=deployment(),
        request_id="r",
        latency_ms=1,
        normalize_vectors=False,
    )
    second = normalize_embedding_batch(
        raw_batch([0, 1, 0]),
        input_count=1,
        index_offset=0,
        logical_model="primary",
        embedding_space="space",
        deployment=deployment(),
        request_id="r",
        latency_ms=1,
        normalize_vectors=False,
    )
    merged = merge_embedding_batches(
        [first, second], request_id="final", total_latency_ms=5, retry_count=2
    )
    assert [item.index for item in merged.embeddings] == [0, 1]
    assert merged.usage.prompt_tokens == 2
    assert merged.estimated_cost_usd == 0.04
    assert merged.retry_count == 2
    with pytest.raises(HarborEmbedMalformedResponseError):
        merge_embedding_batches([], request_id="r", total_latency_ms=1, retry_count=0)
    changed = second.model_copy(update={"deployment": "other"})
    with pytest.raises(HarborEmbedMalformedResponseError):
        merge_embedding_batches([first, changed], request_id="r", total_latency_ms=1, retry_count=0)


def test_embedding_batch_accumulator_hides_partial_results() -> None:
    request = HarborEmbedRequest(
        inputs=("a", "b"), logical_model="primary", metadata={"request_id": "req"}
    )
    accumulator = EmbeddingBatchAccumulator("primary", "space", deployment(), request)
    accumulator.add(raw_batch([1, 0, 0]), offset=0, size=1, latency_ms=1)
    error = HarborEmbedProviderError("provider", retryable=True)
    partial = accumulator.failure(error, batch_index=1, completed=1)
    assert isinstance(partial, HarborEmbedPartialBatchError)
    assert accumulator.failure(error, batch_index=0, completed=0) is error
    accumulator.add(raw_batch([0, 1, 0]), offset=1, size=1, latency_ms=1)
    assert len(accumulator.complete(3).embeddings) == 2
    missing_id = EmbeddingBatchAccumulator(
        "primary", "space", deployment(), HarborEmbedRequest(inputs=("x",))
    )
    with pytest.raises(RuntimeError):
        _ = missing_id.request_id


def test_embedding_input_normalization_variants_and_errors() -> None:
    assert normalize_embedding_inputs("x") == ("x",)
    assert normalize_embedding_inputs([1, 2]) == ((1, 2),)
    assert normalize_embedding_inputs(["a", "b"]) == ("a", "b")
    assert normalize_embedding_inputs(["a", [1, 2]]) == ("a", (1, 2))
    for bad in ([], [True], [object()], [[1, "bad"]]):
        with pytest.raises((TypeError, ValueError)):
            normalize_embedding_inputs(bad)


def test_build_prepare_defaults_and_identity() -> None:
    request = build_embed_request(
        ["a", "b"],
        request=None,
        model="primary",
        request_kwargs={"metadata": {"embedding_purpose": "query"}},
    )
    assert request.metadata.embedding_purpose is EmbeddingPurpose.QUERY
    existing = HarborEmbedRequest(inputs=("x",), logical_model="old")
    assert (
        build_embed_request(None, request=existing, model="new", request_kwargs={}).logical_model
        == "new"
    )
    with pytest.raises(HarborEmbedInvalidRequestError):
        build_embed_request(["x"], request=existing, model=None, request_kwargs={})
    with pytest.raises(HarborEmbedInvalidRequestError):
        build_embed_request(None, request=None, model=None, request_kwargs={})
    defaults = HarborEmbedDefaults(
        dimensions=3,
        encoding_format=EmbeddingEncodingFormat.FLOAT,
        purpose=EmbeddingPurpose.DOCUMENT,
        normalize=True,
        batch_size=2,
    )
    defaulted = apply_embed_defaults(HarborEmbedRequest(inputs=("x",)), defaults)
    assert defaulted.dimensions == 3 and defaulted.purpose is EmbeddingPurpose.DOCUMENT
    identified = ensure_embed_request_id(defaulted)
    assert identified.metadata.request_id and ensure_embed_request_id(identified) is identified
    logical, selected, prepared = prepare_embed_request(
        embed_config(), ["a"], request=None, model=None, request_kwargs={}
    )
    assert logical == "primary" and selected.name == "embed-a" and prepared.metadata.request_id
    with pytest.raises(HarborEmbedConfigurationError):
        prepare_embed_request(
            embed_config(), ["a"], request=None, model="missing", request_kwargs={}
        )


def test_embedding_parameters_batch_limits_and_purpose_mapping() -> None:
    request = HarborEmbedRequest(
        inputs=("a", (1, 2)),
        logical_model="primary",
        metadata=HarborEmbedMetadata(request_id="r", user_id="u"),
        dimensions=3,
        encoding_format=EmbeddingEncodingFormat.FLOAT,
        purpose=EmbeddingPurpose.QUERY,
        custom_headers={"X-Request": SecretStr("two")},
        extra_params={"timeout_override": 1},
        batch_size=5,
    )
    cohere = deployment(provider=HarborEmbedProvider.COHERE, model="cohere/embed")
    params = build_litellm_parameters(
        cohere,
        request,
        inputs=litellm_inputs(request),
        timeout=5,
        litellm_provider="cohere",
    )
    assert params["input_type"] == "search_query"
    assert params["extra_headers"] == {"X-Deploy": "one", "X-Request": "two"}
    assert params["input"] == ["a", [1, 2]]
    assert effective_batch_size(embed_config(deployments=(cohere,)), cohere, request) == 3
    no_batch = deployment(capabilities=HarborEmbedCapabilities(batch=False, default_dimensions=3))
    assert effective_batch_size(embed_config(deployments=(no_batch,)), no_batch, request) == 1
    for provider, key, value in [
        (HarborEmbedProvider.VOYAGE, "input_type", "query"),
        (HarborEmbedProvider.GEMINI, "task_type", "RETRIEVAL_QUERY"),
        (HarborEmbedProvider.OPENAI, None, None),
    ]:
        item = deployment(provider=provider, model=f"{provider.value}/embed")
        rendered = build_litellm_parameters(item, request, inputs=["a"], timeout=1)
        assert (rendered.get(key) if key else None) == value
    with pytest.raises(HarborEmbedInvalidRequestError):
        build_litellm_parameters(
            deployment(),
            request.model_copy(update={"extra_params": {"model": "bad"}}),
            inputs=["a"],
            timeout=1,
        )


def test_embedding_request_capability_and_security_validation() -> None:
    limited = deployment(
        capabilities=HarborEmbedCapabilities(
            batch=True, default_dimensions=3, encoding_format=False
        )
    )
    config = embed_config(deployments=(limited,))
    same_dimensions = HarborEmbedRequest(inputs=("x",), dimensions=3)
    assert validate_embed_request(same_dimensions, config, limited).dimensions is None
    float_format = HarborEmbedRequest(inputs=("x",), encoding_format=EmbeddingEncodingFormat.FLOAT)
    assert validate_embed_request(float_format, config, limited).encoding_format is None
    invalid = [
        HarborEmbedRequest(inputs=((1, 2),)),
        HarborEmbedRequest(inputs=("x",), dimensions=2),
        HarborEmbedRequest(inputs=("x",), encoding_format=EmbeddingEncodingFormat.BASE64),
        HarborEmbedRequest(inputs=("x",), purpose=EmbeddingPurpose.QUERY),
        HarborEmbedRequest(inputs=("x",), extra_params={"model": "bad"}),
        HarborEmbedRequest(inputs=("x",), custom_headers={"Authorization": "bad"}),
    ]
    for request in invalid:
        with pytest.raises((HarborEmbedCapabilityError, HarborEmbedInvalidRequestError)):
            validate_embed_request(request, config, limited)
    tiny = config.model_copy(update={"max_inputs_per_request": 1, "max_characters_per_input": 1})
    with pytest.raises(HarborEmbedInvalidRequestError):
        validate_embed_request(HarborEmbedRequest(inputs=("a", "b")), tiny, limited)
    with pytest.raises(HarborEmbedInvalidRequestError):
        validate_embed_request(HarborEmbedRequest(inputs=("too long",)), tiny, limited)
