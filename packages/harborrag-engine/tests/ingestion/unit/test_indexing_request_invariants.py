"""Generation-context guards on `IndexingRequest` and `ChunkManifest`.

`IndexingRequest.__post_init__` is the last place a mismatched tenant, an
unvalidated manifest, or a resume checkpoint belonging to a different artifact
can be caught before indexing mutates vector and graph stores. `ChunkManifest`
guards the counter/reference agreement the manifest is trusted for.
"""

from __future__ import annotations

import dataclasses

import pytest

from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_engine.ingestion.chunking.schemas import (
    ChunkManifest,
    ChunkValidationResult,
)
from harborrag_engine.ingestion.indexing import IndexingRequest
from harborrag_engine.ingestion.indexing.schemas import (
    GenerationActivationPlan,
    IndexingDiagnostics,
    IndexingResult,
    IndexingStatus,
)

from .indexing_helpers import (
    make_chunking_result,
    make_config,
    make_index_request,
    make_manifest,
    make_record,
    make_reference,
)

REFERENCE = make_reference("logical-1", "revision-1", "hash-1", ordinal=0)


def _manifest(**changes: object) -> ChunkManifest:
    manifest = make_manifest([REFERENCE], artifact_revision_id="artifact-revision-1")
    return dataclasses.replace(manifest, **changes) if changes else manifest


def _records(manifest: ChunkManifest):
    return (make_record(REFERENCE, artifact_revision_id=manifest.artifact_revision_id),)


def _request(**changes: object) -> IndexingRequest:
    manifest = _manifest()
    return make_index_request(proposed=manifest, records=_records(manifest), **changes)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# ChunkManifest counters
# --------------------------------------------------------------------------


def test_manifest_requires_non_empty_identity_values() -> None:
    with pytest.raises(ValueError, match="identity values must be non-empty"):
        _manifest(tenant_id="")
    with pytest.raises(ValueError, match="identity values must be non-empty"):
        _manifest(fingerprint="")


def test_manifest_counters_must_agree_with_their_references() -> None:
    with pytest.raises(ValueError, match="total_chunk_count does not match references"):
        _manifest(total_chunk_count=99)
    with pytest.raises(ValueError, match="total_token_count does not match references"):
        _manifest(total_token_count=99)


def test_a_consistent_manifest_is_accepted() -> None:
    manifest = _manifest()

    assert manifest.total_chunk_count == 1
    assert manifest.total_token_count == REFERENCE.token_count


# --------------------------------------------------------------------------
# IndexingRequest
# --------------------------------------------------------------------------


def test_a_well_formed_request_is_accepted() -> None:
    request = _request()

    assert request.generation_id
    assert request.active_manifest is None
    assert request.resume_result is None


def test_generation_id_must_be_present() -> None:
    manifest = _manifest()
    chunking = make_chunking_result(manifest, _records(manifest))

    with pytest.raises(ValueError, match="generation_id must be non-empty"):
        IndexingRequest(
            chunking=chunking,
            generation_id="   ",
            config=make_config(),
            context=StorageOperationContext(tenant_id=manifest.tenant_id),
        )


def test_an_unvalidated_manifest_is_refused() -> None:
    manifest = _manifest(
        validation=ChunkValidationResult(valid=False, errors=("chunk[0] content is blank",))
    )
    chunking = make_chunking_result(manifest, _records(manifest))

    with pytest.raises(ValueError, match="requires a validated chunk manifest"):
        IndexingRequest(
            chunking=chunking,
            generation_id="generation-1",
            config=make_config(),
            context=StorageOperationContext(tenant_id=manifest.tenant_id),
        )


def test_the_storage_context_tenant_must_match_the_manifest() -> None:
    manifest = _manifest()
    chunking = make_chunking_result(manifest, _records(manifest))

    with pytest.raises(ValueError, match="storage context tenant does not match"):
        IndexingRequest(
            chunking=chunking,
            generation_id="generation-1",
            config=make_config(),
            context=StorageOperationContext(tenant_id="tenant-other"),
        )


def test_the_active_manifest_must_describe_the_same_artifact() -> None:
    other_tenant = make_manifest(
        [REFERENCE],
        artifact_revision_id="artifact-revision-0",
        tenant_id="tenant-other",
    )
    other_artifact = make_manifest(
        [REFERENCE],
        artifact_revision_id="artifact-revision-0",
        artifact_id="artifact-other",
    )

    with pytest.raises(ValueError, match="must describe one artifact"):
        _request(active=other_tenant)
    with pytest.raises(ValueError, match="must describe one artifact"):
        _request(active=other_artifact)


def test_active_metadata_without_an_active_manifest_is_refused() -> None:
    with pytest.raises(ValueError, match="requires an active manifest"):
        _request(active_fingerprint="embed-1")
    with pytest.raises(ValueError, match="requires an active manifest"):
        _request(active_generation_id="generation-0")


def test_an_active_manifest_carries_its_metadata() -> None:
    active = make_manifest([REFERENCE], artifact_revision_id="artifact-revision-0")

    request = _request(
        active=active,
        active_fingerprint="embed-1",
        active_generation_id="generation-0",
    )

    assert request.active_manifest is active
    assert request.active_generation_id == "generation-0"


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_id": "artifact-other"},
        {"artifact_revision_id": "artifact-revision-other"},
        {"generation_id": "generation-other"},
    ],
)
def test_a_resume_checkpoint_from_another_run_is_refused(changes: dict[str, str]) -> None:
    """A resume checkpoint must belong to exactly this artifact and generation."""
    base = _request()
    resume = dataclasses.replace(
        _fake_result(base),
        **changes,
    )

    with pytest.raises(ValueError, match="resume checkpoint does not match the request"):
        dataclasses.replace(base, resume_result=resume)


def _fake_result(request: IndexingRequest) -> IndexingResult:
    manifest = request.chunking.manifest
    return IndexingResult(
        artifact_id=manifest.artifact_id,
        artifact_revision_id=manifest.artifact_revision_id,
        generation_id=request.generation_id,
        status=IndexingStatus.PARTIAL,
        vector_valid=True,
        graph_valid=False,
        validation_errors=(),
        diagnostics=IndexingDiagnostics(
            new_chunks=1,
            unchanged_chunks=0,
            changed_chunks=0,
            removed_chunks=0,
            reembedded_chunks=0,
            embedded_chunks=1,
            embedding_batches=1,
            vector_upserts=1,
            vector_retentions=0,
            vector_retirements=0,
            vector_deletions=0,
            vector_tombstones=0,
            graph_nodes=0,
            graph_edges=0,
        ),
        activation=GenerationActivationPlan(
            artifact_id=manifest.artifact_id,
            generation_id=request.generation_id,
            previous_generation_id=None,
            vector_collection="documents",
            activate_vector_ids=("v1",),
            retire_vector_ids=(),
            delete_vector_ids=(),
            tombstone_vector_ids=(),
        ),
    )


def test_a_matching_resume_checkpoint_is_accepted() -> None:
    base = _request()

    resumed = dataclasses.replace(base, resume_result=_fake_result(base))

    assert resumed.resume_result is not None
    assert resumed.resume_result.generation_id == base.generation_id
