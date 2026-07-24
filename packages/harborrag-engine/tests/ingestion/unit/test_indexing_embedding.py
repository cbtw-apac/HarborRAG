from __future__ import annotations

from typing import Any

import pytest

from harborrag_core.models.embed import (
    HarborEmbedding,
    HarborEmbedRequest,
    HarborEmbedResponse,
)
from harborrag_engine.ingestion.indexing import (
    EmbeddingBatch,
    EmbeddingBatchPlanner,
    EmbeddingInputPreparer,
    EmbeddingResultMismatchError,
    EmbeddingService,
    IncrementalChunkDiffer,
    PreparedEmbeddingInput,
)

from .indexing_helpers import (
    CharacterCounter,
    make_config,
    make_manifest,
    make_record,
    make_reference,
)


class IncompleteEmbedClient:
    async def aembed(
        self,
        inputs: Any = None,
        *,
        request: HarborEmbedRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborEmbedResponse:
        del inputs, model, kwargs
        assert request is not None
        return HarborEmbedResponse(
            embeddings=(HarborEmbedding(index=0, value=(1.0, 0.0), dimensions=2),),
            logical_model="documents",
            embedding_space="documents-v1",
            provider="fake",
            provider_model="fake-embed",
            deployment="test",
            request_id="incomplete",
        )

    async def aclose(self) -> None:
        return None


class DuplicateEmbedClient:
    async def aembed(
        self,
        inputs: Any = None,
        *,
        request: HarborEmbedRequest | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> HarborEmbedResponse:
        del inputs, model, kwargs
        assert request is not None
        return HarborEmbedResponse(
            embeddings=(
                HarborEmbedding(index=0, value=(1.0, 0.0), dimensions=2),
                HarborEmbedding(index=1, value=(0.0, 1.0), dimensions=2),
                HarborEmbedding(index=1, value=(0.5, 0.5), dimensions=2),
            ),
            logical_model="documents",
            embedding_space="documents-v1",
            provider="fake",
            provider_model="fake-embed",
            deployment="test",
            request_id="duplicate",
        )

    async def aclose(self) -> None:
        return None


def test_embedding_preparation_adds_bounded_context_without_mutating_content() -> None:
    reference = make_reference("logical-1", "revision-1", "hash-1", ordinal=0)
    record = make_record(
        reference,
        artifact_revision_id="artifact-revision-1",
        content="canonical body",
        metadata={
            "source_kind": "jira",
            "issue_key": "HARBOR-1",
            "issue_summary": "A very long issue summary",
        },
    )
    config = make_config(embedding_context_maximum_characters=24)

    prepared = EmbeddingInputPreparer(CharacterCounter()).prepare((record,), config)[0]

    context, content = prepared.text.split("\n\n", 1)
    assert len(context) <= 24
    assert content == "canonical body"
    assert record.content == "canonical body"


def test_embedding_fingerprint_tracks_rendering_inputs_not_batching_limits() -> None:
    base = make_config()
    changed_context = make_config(embedding_context_maximum_characters=64)
    changed_batch = make_config(embedding_batch_size=1)

    assert (
        base.embedding_configuration_fingerprint
        != changed_context.embedding_configuration_fingerprint
    )
    assert (
        base.embedding_configuration_fingerprint
        == changed_batch.embedding_configuration_fingerprint
    )
    assert base.configuration_fingerprint != changed_batch.configuration_fingerprint


def test_embedding_batch_planner_respects_item_and_token_limits() -> None:
    references = (
        make_reference("logical-1", "revision-1", "hash-1", ordinal=0),
        make_reference("logical-2", "revision-2", "hash-2", ordinal=1),
        make_reference("logical-3", "revision-3", "hash-3", ordinal=2),
    )
    manifest = make_manifest(references, artifact_revision_id="artifact-revision-1")
    records = tuple(
        make_record(reference, artifact_revision_id="artifact-revision-1")
        for reference in references
    )
    prepared = tuple(
        PreparedEmbeddingInput(record=record, text=record.content, token_count=count)
        for record, count in zip(records, (3, 3, 2), strict=True)
    )
    diff = IncrementalChunkDiffer().compare(
        manifest,
        None,
        target_embedding_configuration_fingerprint="embed-v1",
    )
    config = make_config(embedding_batch_size=2, maximum_embedding_batch_tokens=5)

    batches = EmbeddingBatchPlanner().plan(diff, prepared, config)

    assert [[str(item.record.chunk_revision_id) for item in batch.inputs] for batch in batches] == [
        ["revision-1"],
        ["revision-2", "revision-3"],
    ]
    assert [batch.total_tokens for batch in batches] == [3, 5]


def test_embedding_batch_planner_rejects_individually_oversized_chunks() -> None:
    reference = make_reference("logical-1", "revision-1", "hash-1", ordinal=0)
    manifest = make_manifest((reference,), artifact_revision_id="artifact-revision-1")
    record = make_record(reference, artifact_revision_id="artifact-revision-1")
    prepared = PreparedEmbeddingInput(record=record, text=record.content, token_count=6)
    diff = IncrementalChunkDiffer().compare(
        manifest,
        None,
        target_embedding_configuration_fingerprint="embed-v1",
    )

    with pytest.raises(ValueError, match="exceeds embedding batch token limit"):
        EmbeddingBatchPlanner().plan(
            diff,
            (prepared,),
            make_config(maximum_embedding_batch_tokens=5),
        )


@pytest.mark.asyncio
async def test_embedding_service_rejects_incomplete_provider_responses() -> None:
    references = (
        make_reference("logical-1", "revision-1", "hash-1", ordinal=0),
        make_reference("logical-2", "revision-2", "hash-2", ordinal=1),
    )
    records = tuple(
        make_record(reference, artifact_revision_id="artifact-revision-1")
        for reference in references
    )
    inputs = tuple(
        PreparedEmbeddingInput(record=record, text=record.content, token_count=4)
        for record in records
    )
    batch = EmbeddingBatch(ordinal=0, inputs=inputs, total_tokens=8)

    with pytest.raises(EmbeddingResultMismatchError, match="incomplete or duplicate"):
        await EmbeddingService(IncompleteEmbedClient()).embed(
            (batch,),
            make_config(embedding_dimensions=2),
        )


@pytest.mark.asyncio
async def test_embedding_service_rejects_duplicate_provider_indices() -> None:
    references = (
        make_reference("logical-1", "revision-1", "hash-1", ordinal=0),
        make_reference("logical-2", "revision-2", "hash-2", ordinal=1),
    )
    inputs = tuple(
        PreparedEmbeddingInput(
            record=make_record(reference, artifact_revision_id="artifact-revision-1"),
            text=f"text-{reference.ordinal}",
            token_count=6,
        )
        for reference in references
    )
    batch = EmbeddingBatch(ordinal=0, inputs=inputs, total_tokens=12)

    with pytest.raises(EmbeddingResultMismatchError, match="incomplete or duplicate"):
        await EmbeddingService(DuplicateEmbedClient()).embed(
            (batch,),
            make_config(embedding_dimensions=2),
        )
