"""Rejection paths for the indexing schema invariants.

The indexing dataclasses are frozen and validate in ``__post_init__``, so every
guard below is the only thing standing between a malformed diff/embedding/
activation plan and a provider mutation. The happy paths are covered by the
service suites; this module pins the rejections.
"""

from __future__ import annotations

import pytest

from harborrag_engine.ingestion.chunking.schemas import ChunkReference
from harborrag_engine.ingestion.indexing.schemas import (
    ChunkDiffEntry,
    ChunkDiffResult,
    ChunkDiffStatus,
    EmbeddedChunk,
    EmbeddingBatch,
    EmbeddingRun,
    GenerationActivationPlan,
    PreparedEmbeddingInput,
)

REF = ChunkReference(
    logical_chunk_id="logical-1",
    chunk_revision_id="rev-1",
    ordinal=0,
    content_hash="hash-1",
    token_count=4,
)
OTHER_REF = ChunkReference(
    logical_chunk_id="logical-2",
    chunk_revision_id="rev-2",
    ordinal=1,
    content_hash="hash-2",
    token_count=4,
)


def _entry(status: ChunkDiffStatus, **overrides: object) -> ChunkDiffEntry:
    fields: dict[str, object] = {
        "logical_chunk_id": "logical-1",
        "status": status,
        "previous": REF,
        "current": REF,
    }
    fields.update(overrides)
    return ChunkDiffEntry(**fields)  # type: ignore[arg-type]


def _plan(**overrides: object) -> GenerationActivationPlan:
    fields: dict[str, object] = {
        "artifact_id": "artifact-1",
        "generation_id": "gen-2",
        "previous_generation_id": "gen-1",
        "vector_collection": "chunks",
        "activate_vector_ids": ("v1",),
        "retire_vector_ids": (),
        "delete_vector_ids": (),
        "tombstone_vector_ids": (),
    }
    fields.update(overrides)
    return GenerationActivationPlan(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# ChunkDiffEntry
# --------------------------------------------------------------------------


def test_diff_entry_requires_a_logical_id() -> None:
    with pytest.raises(ValueError, match="logical_chunk_id must be non-empty"):
        _entry(ChunkDiffStatus.CHANGED, logical_chunk_id="   ")


def test_diff_entry_requires_at_least_one_reference() -> None:
    with pytest.raises(ValueError, match="requires a previous or current reference"):
        _entry(ChunkDiffStatus.CHANGED, previous=None, current=None)


def test_diff_entry_references_must_match_its_identity() -> None:
    with pytest.raises(ValueError, match="does not match logical_chunk_id"):
        _entry(ChunkDiffStatus.CHANGED, current=OTHER_REF)


def test_new_entry_rejects_a_previous_reference() -> None:
    with pytest.raises(ValueError, match="NEW requires only a current reference"):
        _entry(ChunkDiffStatus.NEW)
    with pytest.raises(ValueError, match="NEW requires only a current reference"):
        _entry(ChunkDiffStatus.NEW, previous=REF, current=None)


def test_removed_entry_rejects_a_current_reference() -> None:
    with pytest.raises(ValueError, match="REMOVED requires only a previous reference"):
        _entry(ChunkDiffStatus.REMOVED)


def test_paired_statuses_require_both_references() -> None:
    with pytest.raises(ValueError, match="requires both references"):
        _entry(ChunkDiffStatus.CHANGED, previous=None)
    with pytest.raises(ValueError, match="requires both references"):
        _entry(ChunkDiffStatus.CHANGED, current=None)


def test_valid_new_and_removed_entries_are_accepted() -> None:
    new = _entry(ChunkDiffStatus.NEW, previous=None)
    removed = _entry(ChunkDiffStatus.REMOVED, current=None)

    assert new.status is ChunkDiffStatus.NEW
    assert removed.status is ChunkDiffStatus.REMOVED
    assert removed.requires_embedding is False


# --------------------------------------------------------------------------
# ChunkDiffResult projections
# --------------------------------------------------------------------------


def test_diff_result_projections_preserve_manifest_order() -> None:
    new = _entry(ChunkDiffStatus.NEW, previous=None)
    removed = _entry(ChunkDiffStatus.REMOVED, current=None)
    unchanged = _entry(ChunkDiffStatus.UNCHANGED)
    result = ChunkDiffResult(
        entries=(new, unchanged, removed),
        active_manifest_fingerprint="active",
        proposed_manifest_fingerprint="proposed",
        active_embedding_configuration_fingerprint=None,
        target_embedding_configuration_fingerprint="embed-1",
    )

    assert result.removed == (removed,)
    assert result.count(ChunkDiffStatus.UNCHANGED) == 1
    assert result.count(ChunkDiffStatus.NEW) == 1
    assert all(entry.requires_embedding for entry in result.for_embedding)


# --------------------------------------------------------------------------
# Embedding inputs
# --------------------------------------------------------------------------


def test_prepared_input_rejects_blank_text_or_zero_tokens() -> None:
    with pytest.raises(ValueError, match="text/tokens are invalid"):
        PreparedEmbeddingInput(record=None, text="   ", token_count=4)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="text/tokens are invalid"):
        PreparedEmbeddingInput(record=None, text="body", token_count=0)  # type: ignore[arg-type]


def test_embedding_batch_rejects_bad_ordinal_or_token_total() -> None:
    item = PreparedEmbeddingInput(record=None, text="body", token_count=3)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ordinal/inputs are invalid"):
        EmbeddingBatch(ordinal=-1, inputs=(item,), total_tokens=3)
    with pytest.raises(ValueError, match="ordinal/inputs are invalid"):
        EmbeddingBatch(ordinal=0, inputs=(), total_tokens=0)
    with pytest.raises(ValueError, match="token total does not match inputs"):
        EmbeddingBatch(ordinal=0, inputs=(item,), total_tokens=99)

    assert EmbeddingBatch(ordinal=0, inputs=(item,), total_tokens=3).total_tokens == 3


def test_embedded_chunk_requires_a_vector() -> None:
    with pytest.raises(ValueError, match="vector must not be empty"):
        EmbeddedChunk(record=None, vector=())  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# EmbeddingRun
# --------------------------------------------------------------------------


def test_embedding_run_requires_a_fingerprint() -> None:
    with pytest.raises(ValueError, match="fingerprint must be non-empty"):
        EmbeddingRun(
            chunks=(),
            configuration_fingerprint="  ",
            dimension=None,
            embedding_space=None,
        )


def test_embedding_run_rejects_mixed_dimensions() -> None:
    chunks = (
        EmbeddedChunk(record=None, vector=(0.1, 0.2)),  # type: ignore[arg-type]
        EmbeddedChunk(record=None, vector=(0.1, 0.2, 0.3)),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="dimensions are inconsistent"):
        EmbeddingRun(
            chunks=chunks,
            configuration_fingerprint="embed-1",
            dimension=2,
            embedding_space="space",
        )


def test_embedding_run_rejects_a_declared_dimension_mismatch() -> None:
    chunks = (EmbeddedChunk(record=None, vector=(0.1, 0.2)),)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="dimensions are inconsistent"):
        EmbeddingRun(
            chunks=chunks,
            configuration_fingerprint="embed-1",
            dimension=3,
            embedding_space="space",
        )


def test_non_empty_embedding_run_requires_an_embedding_space() -> None:
    chunks = (EmbeddedChunk(record=None, vector=(0.1, 0.2)),)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="requires an embedding space"):
        EmbeddingRun(
            chunks=chunks,
            configuration_fingerprint="embed-1",
            dimension=2,
            embedding_space=None,
        )


def test_empty_embedding_run_must_not_declare_vector_metadata() -> None:
    with pytest.raises(ValueError, match="cannot declare vector metadata"):
        EmbeddingRun(
            chunks=(),
            configuration_fingerprint="embed-1",
            dimension=4,
            embedding_space=None,
        )

    empty = EmbeddingRun(
        chunks=(),
        configuration_fingerprint="embed-1",
        dimension=None,
        embedding_space=None,
    )
    assert empty.chunks == ()


# --------------------------------------------------------------------------
# GenerationActivationPlan
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"artifact_id": " "}, "artifact_id must be non-empty"),
        ({"generation_id": " "}, "generation_id must be non-empty"),
        ({"vector_collection": " "}, "vector_collection must be non-empty"),
        (
            {"previous_generation_id": "gen-2"},
            "previous and proposed generations must differ",
        ),
        (
            {"activate_vector_ids": ("v1", "  ")},
            "vector activation identities must be non-empty",
        ),
        (
            {"activate_vector_ids": ("v1",), "retire_vector_ids": ("v1",)},
            "cannot have multiple activation actions",
        ),
    ],
)
def test_activation_plan_rejects_contradictory_identities(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _plan(**overrides)


def test_activation_plan_accepts_a_first_generation() -> None:
    plan = _plan(previous_generation_id=None, activate_vector_ids=("v1", "v2"))

    assert plan.previous_generation_id is None
    assert plan.activate_vector_ids == ("v1", "v2")
