from __future__ import annotations

import pytest

from harborrag_core.schemas.vector import VectorPoint
from harborrag_engine.ingestion.indexing import (
    ChunkDiffStatus,
    RemovedVectorPolicy,
    VectorIndexService,
    VectorIndexValidationError,
    VectorMutationAction,
    deterministic_vector_point_id,
)

from .indexing_helpers import (
    CharacterCounter,
    FakeEmbedClient,
    FakeVectorRepository,
    make_config,
    make_index_request,
    make_manifest,
    make_record,
    make_reference,
)


def make_service(embed_client, vector_repository) -> VectorIndexService:
    return VectorIndexService(
        embed_client=embed_client,
        vector_repository=vector_repository,  # type: ignore[arg-type]
        token_counter=CharacterCounter(),
    )


@pytest.mark.asyncio
async def test_vector_index_service_stages_incremental_mutations_and_metadata() -> None:
    active_references = (
        make_reference("logical-same", "revision-same", "hash-same", ordinal=0),
        make_reference("logical-change", "revision-old", "hash-old", ordinal=1),
        make_reference("logical-remove", "revision-remove", "hash-remove", ordinal=2),
    )
    proposed_references = (
        make_reference("logical-same", "revision-same", "hash-same", ordinal=0),
        make_reference("logical-change", "revision-new", "hash-new", ordinal=1),
        make_reference(
            "logical-new",
            "revision-added",
            "hash-added",
            ordinal=2,
            body_uri="memory://chunks/revision-added",
        ),
    )
    active = make_manifest(active_references, artifact_revision_id="revision-1")
    proposed = make_manifest(proposed_references, artifact_revision_id="revision-2")
    records = tuple(
        make_record(reference, artifact_revision_id="revision-2")
        for reference in proposed_references
    )
    config = make_config(embedding_batch_size=1)
    embed_client = FakeEmbedClient()
    vector_repository = FakeVectorRepository()
    request = make_index_request(
        proposed=proposed,
        records=records,
        active=active,
        active_fingerprint=config.embedding_configuration_fingerprint,
        active_generation_id="generation-1",
        config=config,
    )

    result = await make_service(embed_client, vector_repository).stage(request)

    assert [entry.status for entry in result.diff.entries] == [
        ChunkDiffStatus.UNCHANGED,
        ChunkDiffStatus.CHANGED,
        ChunkDiffStatus.NEW,
        ChunkDiffStatus.REMOVED,
    ]
    assert len(embed_client.requests) == 2
    assert [mutation.action for mutation in result.plan.mutations] == [
        VectorMutationAction.RETAIN,
        VectorMutationAction.RETIRE,
        VectorMutationAction.UPSERT,
        VectorMutationAction.UPSERT,
        VectorMutationAction.RETIRE,
    ]
    assert len(result.plan.points) == 2
    assert result.validation.valid
    assert vector_repository.specs[0].dimension == 3
    retained = result.plan.mutations[0]
    assert retained.point_id is not None
    for point in result.plan.points:
        assert point.payload["tenant_id"] == "tenant-1"
        assert point.payload["artifact_id"] == "artifact-1"
        assert point.payload["artifact_revision_id"] == "revision-2"
        assert point.payload["generation_id"] == "generation-2"
        assert point.payload["source_kind"] == "document"
        assert point.payload["structural_path"] == ["Guide", "Setup"]
        assert point.payload["page_range"] == [1, 1]
        assert point.payload["embedding_configuration_fingerprint"] == (
            config.embedding_configuration_fingerprint
        )
        assert point.payload["index_state"] == "staged"
        assert point.payload["is_active"] is False
        assert "content" not in point.payload
    references_by_logical_id = {
        point.payload["logical_chunk_id"]: point.payload["content_reference"]
        for point in result.plan.points
    }
    assert references_by_logical_id == {
        "logical-change": "harborrag:chunk:revision-new",
        "logical-new": "memory://chunks/revision-added",
    }


@pytest.mark.asyncio
async def test_vector_index_service_reembeds_when_configuration_changes() -> None:
    reference = make_reference("logical-1", "revision-1", "hash-1", ordinal=0)
    active = make_manifest((reference,), artifact_revision_id="revision-1")
    proposed = make_manifest((reference,), artifact_revision_id="revision-2")
    record = make_record(reference, artifact_revision_id="revision-2")
    old_config = make_config(embedding_text_rendering_version="1")
    new_config = make_config(embedding_text_rendering_version="2")
    embed_client = FakeEmbedClient()

    result = await make_service(embed_client, FakeVectorRepository()).stage(
        make_index_request(
            proposed=proposed,
            records=(record,),
            active=active,
            active_fingerprint=old_config.embedding_configuration_fingerprint,
            active_generation_id="generation-1",
            config=new_config,
        )
    )

    assert result.diff.entries[0].status is ChunkDiffStatus.REEMBED_REQUIRED
    assert len(embed_client.requests) == 1
    assert result.plan.count(VectorMutationAction.RETIRE) == 1
    assert result.plan.count(VectorMutationAction.UPSERT) == 1


@pytest.mark.asyncio
async def test_vector_index_service_does_not_embed_or_write_unchanged_chunks() -> None:
    reference = make_reference("logical-1", "revision-1", "hash-1", ordinal=0)
    active = make_manifest((reference,), artifact_revision_id="revision-1")
    proposed = make_manifest((reference,), artifact_revision_id="revision-2")
    record = make_record(reference, artifact_revision_id="revision-2")
    config = make_config()
    embed_client = FakeEmbedClient()
    repository = FakeVectorRepository()

    result = await make_service(embed_client, repository).stage(
        make_index_request(
            proposed=proposed,
            records=(record,),
            active=active,
            active_fingerprint=config.embedding_configuration_fingerprint,
            active_generation_id="generation-1",
            config=config,
        )
    )

    assert result.plan.points == ()
    assert result.plan.count(VectorMutationAction.RETAIN) == 1
    assert embed_client.requests == []
    assert repository.upsert_calls == 0


@pytest.mark.asyncio
async def test_vector_index_service_reuses_embedding_when_only_metadata_changes() -> None:
    active_reference = make_reference(
        "logical-1",
        "revision-1",
        "hash-1",
        ordinal=0,
        token_count=20,
    )
    proposed_reference = make_reference(
        "logical-1",
        "revision-2",
        "hash-1",
        ordinal=0,
        token_count=5,
    )
    active = make_manifest((active_reference,), artifact_revision_id="artifact-revision-1")
    proposed = make_manifest(
        (proposed_reference,),
        artifact_revision_id="artifact-revision-2",
    )
    record = make_record(
        proposed_reference,
        artifact_revision_id="artifact-revision-2",
        structural_path=("Guide", "Revised"),
    )
    config = make_config()
    embed_client = FakeEmbedClient()
    repository = FakeVectorRepository()
    old_point_id = deterministic_vector_point_id(
        tenant_id="tenant-1",
        collection=config.vector_collection,
        generation_id="generation-1",
        chunk_revision_id="revision-1",
        embedding_configuration_fingerprint=(config.embedding_configuration_fingerprint),
    )
    repository.points[old_point_id] = VectorPoint(
        id=old_point_id,
        tenant_id="tenant-1",
        vector=[0.1, 0.2, 0.3],
        payload={"is_active": True},
    )

    result = await make_service(embed_client, repository).stage(
        make_index_request(
            proposed=proposed,
            records=(record,),
            active=active,
            active_fingerprint=config.embedding_configuration_fingerprint,
            active_generation_id="generation-1",
            config=config,
        )
    )

    assert result.diff.entries[0].status is ChunkDiffStatus.REFRESH_REQUIRED
    assert embed_client.requests == []
    assert result.batches == ()
    assert result.plan.count(VectorMutationAction.RETIRE) == 1
    assert result.plan.count(VectorMutationAction.UPSERT) == 1
    refreshed = result.plan.points[0]
    assert refreshed.vector == [0.1, 0.2, 0.3]
    assert refreshed.payload["generation_id"] == "generation-2"
    assert refreshed.payload["chunk_revision_id"] == "revision-2"
    assert refreshed.payload["token_count"] == 5
    assert refreshed.payload["structural_path"] == ["Guide", "Revised"]


@pytest.mark.parametrize(
    ("policy", "action"),
    [
        (RemovedVectorPolicy.RETIRE, VectorMutationAction.RETIRE),
        (RemovedVectorPolicy.DELETE, VectorMutationAction.DELETE),
        (RemovedVectorPolicy.TOMBSTONE, VectorMutationAction.TOMBSTONE),
    ],
)
@pytest.mark.asyncio
async def test_removed_vector_policy_produces_explicit_lifecycle_action(
    policy: RemovedVectorPolicy,
    action: VectorMutationAction,
) -> None:
    reference = make_reference("logical-1", "revision-1", "hash-1", ordinal=0)
    active = make_manifest((reference,), artifact_revision_id="revision-1")
    proposed = make_manifest((), artifact_revision_id="revision-2")
    config = make_config(removed_vector_policy=policy)

    result = await make_service(FakeEmbedClient(), FakeVectorRepository()).stage(
        make_index_request(
            proposed=proposed,
            records=(),
            active=active,
            active_fingerprint=config.embedding_configuration_fingerprint,
            active_generation_id="generation-1",
            config=config,
        )
    )

    assert result.plan.mutations[0].action is action
    assert result.plan.mutations[0].point_id is not None


@pytest.mark.asyncio
async def test_repeated_vector_indexing_is_idempotent() -> None:
    reference = make_reference("logical-1", "revision-1", "hash-1", ordinal=0)
    proposed = make_manifest((reference,), artifact_revision_id="revision-1")
    record = make_record(reference, artifact_revision_id="revision-1")
    request = make_index_request(proposed=proposed, records=(record,))
    repository = FakeVectorRepository()
    service = make_service(FakeEmbedClient(), repository)

    first = await service.stage(request)
    second = await service.stage(request)

    assert first.plan.points[0].id == second.plan.points[0].id
    assert len(repository.points) == 1
    assert repository.upsert_calls == 2


@pytest.mark.asyncio
async def test_vector_validation_rejects_points_activated_during_staging() -> None:
    reference = make_reference("logical-1", "revision-1", "hash-1", ordinal=0)
    proposed = make_manifest((reference,), artifact_revision_id="revision-1")
    record = make_record(reference, artifact_revision_id="revision-1")
    service = make_service(FakeEmbedClient(), FakeVectorRepository(activate_on_read=True))

    with pytest.raises(VectorIndexValidationError, match="became active"):
        await service.stage(make_index_request(proposed=proposed, records=(record,)))


def test_deterministic_vector_point_identity_changes_with_generation() -> None:
    values = {
        "tenant_id": "tenant-1",
        "collection": "documents",
        "generation_id": "generation-2",
        "chunk_revision_id": "revision-1",
        "embedding_configuration_fingerprint": "embed-config-v1",
    }

    first = deterministic_vector_point_id(**values)
    second = deterministic_vector_point_id(**values)
    changed = deterministic_vector_point_id(**{**values, "generation_id": "generation-3"})

    assert first == second
    assert first != changed
