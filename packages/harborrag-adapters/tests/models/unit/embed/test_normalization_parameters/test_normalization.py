from __future__ import annotations

import math

import pytest

from harborrag_adapters.models.embed.normalization import (
    merge_embedding_batches,
    normalize_embedding_batch,
)
from harborrag_core.models.errors import HarborEmbedMalformedResponseError

from .conftest import deployment, raw_batch

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


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
