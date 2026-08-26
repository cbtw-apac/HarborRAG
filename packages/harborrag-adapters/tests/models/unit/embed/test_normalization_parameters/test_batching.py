from __future__ import annotations

import pytest

from harborrag_adapters.models.embed.batching import EmbeddingBatchAccumulator
from harborrag_core.models.embed import HarborEmbedRequest
from harborrag_core.models.errors import HarborEmbedPartialBatchError, HarborEmbedProviderError

from .conftest import deployment, raw_batch

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


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
