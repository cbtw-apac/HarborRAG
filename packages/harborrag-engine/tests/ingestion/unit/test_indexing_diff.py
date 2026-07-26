from __future__ import annotations

import pytest

from harborrag_engine.ingestion.indexing import (
    ChunkDiffError,
    ChunkDiffStatus,
    IncrementalChunkDiffer,
)

from .indexing_helpers import make_manifest, make_reference


def test_incremental_diff_classifies_new_unchanged_changed_and_removed() -> None:
    active = make_manifest(
        (
            make_reference("logical-same", "revision-same", "hash-same", ordinal=0),
            make_reference("logical-change", "revision-old", "hash-old", ordinal=1),
            make_reference("logical-remove", "revision-remove", "hash-remove", ordinal=2),
        ),
        artifact_revision_id="artifact-revision-1",
    )
    proposed = make_manifest(
        (
            make_reference("logical-same", "revision-same", "hash-same", ordinal=0),
            make_reference("logical-change", "revision-new", "hash-new", ordinal=1),
            make_reference("logical-new", "revision-added", "hash-added", ordinal=2),
        ),
        artifact_revision_id="artifact-revision-2",
    )

    result = IncrementalChunkDiffer().compare(
        proposed,
        active,
        target_embedding_configuration_fingerprint="embed-v1",
        active_embedding_configuration_fingerprint="embed-v1",
    )

    assert [(entry.logical_chunk_id, entry.status) for entry in result.entries] == [
        ("logical-same", ChunkDiffStatus.UNCHANGED),
        ("logical-change", ChunkDiffStatus.CHANGED),
        ("logical-new", ChunkDiffStatus.NEW),
        ("logical-remove", ChunkDiffStatus.REMOVED),
    ]
    assert [entry.logical_chunk_id for entry in result.for_embedding] == [
        "logical-change",
        "logical-new",
    ]


def test_incremental_diff_requires_reembedding_when_model_changes() -> None:
    active = make_manifest(
        (make_reference("logical-1", "revision-1", "hash-1", ordinal=0),),
        artifact_revision_id="artifact-revision-1",
    )
    proposed = make_manifest(
        (make_reference("logical-1", "revision-1", "hash-1", ordinal=0),),
        artifact_revision_id="artifact-revision-2",
    )

    result = IncrementalChunkDiffer().compare(
        proposed,
        active,
        target_embedding_configuration_fingerprint="embed-v2",
        active_embedding_configuration_fingerprint="embed-v1",
    )

    assert result.entries[0].status is ChunkDiffStatus.REEMBED_REQUIRED


def test_incremental_diff_refreshes_metadata_without_reembedding_content() -> None:
    active = make_manifest(
        (make_reference("logical-1", "revision-1", "hash-1", ordinal=0),),
        artifact_revision_id="artifact-revision-1",
    )
    proposed = make_manifest(
        (make_reference("logical-1", "revision-2", "hash-1", ordinal=0),),
        artifact_revision_id="artifact-revision-2",
    )

    result = IncrementalChunkDiffer().compare(
        proposed,
        active,
        target_embedding_configuration_fingerprint="embed-v1",
        active_embedding_configuration_fingerprint="embed-v1",
    )

    assert result.entries[0].status is ChunkDiffStatus.REFRESH_REQUIRED
    assert result.for_embedding == ()
    assert result.for_refresh == result.entries


def test_incremental_diff_treats_unknown_active_model_as_reembed_required() -> None:
    active = make_manifest(
        (make_reference("logical-1", "revision-1", "hash-1", ordinal=0),),
        artifact_revision_id="artifact-revision-1",
    )
    proposed = make_manifest(
        (make_reference("logical-1", "revision-1", "hash-1", ordinal=0),),
        artifact_revision_id="artifact-revision-2",
    )

    result = IncrementalChunkDiffer().compare(
        proposed,
        active,
        target_embedding_configuration_fingerprint="embed-v1",
    )

    assert result.entries[0].status is ChunkDiffStatus.REEMBED_REQUIRED


def test_incremental_diff_marks_every_chunk_new_without_active_manifest() -> None:
    proposed = make_manifest(
        (
            make_reference("logical-1", "revision-1", "hash-1", ordinal=0),
            make_reference("logical-2", "revision-2", "hash-2", ordinal=1),
        ),
        artifact_revision_id="artifact-revision-1",
    )

    result = IncrementalChunkDiffer().compare(
        proposed,
        None,
        target_embedding_configuration_fingerprint="embed-v1",
    )

    assert [entry.status for entry in result.entries] == [
        ChunkDiffStatus.NEW,
        ChunkDiffStatus.NEW,
    ]


def test_incremental_diff_rejects_cross_artifact_manifests() -> None:
    active = make_manifest((), artifact_revision_id="revision-1")
    proposed = make_manifest((), artifact_revision_id="revision-2", artifact_id="artifact-2")

    with pytest.raises(ChunkDiffError, match="same tenant artifact"):
        IncrementalChunkDiffer().compare(
            proposed,
            active,
            target_embedding_configuration_fingerprint="embed-v1",
        )
