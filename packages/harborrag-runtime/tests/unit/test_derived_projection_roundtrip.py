"""Real embedded Qdrant precision, payload, and parent-wrapper roundtrips."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from harborrag_adapters.repositories.object_store import (
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
)
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.indexing import VectorDistance, VectorIndexRecord
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import DocumentTopologyBuild
from harborrag_core.topology.derived import ContextualIndexProfile, ContextualManifest
from harborrag_core.topology.permissions import (
    DerivedArtifactLineage,
    DerivedArtifactRecord,
    PermissionDependency,
)
from harborrag_engine.topology.vector_values import canonical_dense_vector
from harborrag_runtime.topology.derived_cleanup import RetiredDerivedProjectionCleaner
from harborrag_runtime.topology.derived_coverage import validate_derived_coverage
from harborrag_runtime.topology.derived_projection import (
    DerivedVectorProjection,
    records_match_at_storage_precision,
)

CONTEXT = StorageOperationContext.system("tenant-1")
PROFILE = ContextualIndexProfile(model="existing", dimension=2, deployment_revision="pinned")
POINT = str(UUID(int=1))


async def frozen(store, *, parent=False, named=False, quantized=True):
    fingerprint = PROFILE.parent_fingerprint if parent else PROFILE.fingerprint
    vector = canonical_dense_vector((0.6, 0.8)) if quantized else (0.6, 0.8)
    row = VectorIndexRecord(
        id=POINT,
        tenant_id="tenant-1",
        vector=list(vector),
        named_vectors={"unconfigured": list(vector)} if named else {},
        payload={
            "record_kind": "parent_description" if parent else "contextual",
            "chunk_id": "chunk",
            "chunk_ids": ["chunk"],
            "cited_chunk_ids": ["chunk"],
            "parent_key": "doc",
            "level": "document",
            "document_id": "doc",
            "document_version_id": "version",
            "build_id": "build",
            "projection_point_id": POINT,
            "embedding_profile": fingerprint,
            "input_coverage": "complete",
            "semantic_coverage": "not_evaluated",
        },
    )
    rows = [row.model_dump(mode="json")]
    payload = {"descriptions": [], "records": rows} if parent else rows
    reference = await ImmutableArtifactWriter(store).put(
        ImmutableArtifact(
            bucket="artifacts",
            key="frozen.json",
            payload=json.dumps(payload).encode(),
            media_type="application/json",
            artifact_kind="derived-vector",
        ),
        context=CONTEXT,
    )
    manifest = ContextualManifest(
        artifact=reference,
        embedding_profile=fingerprint,
        dimension=2,
        point_ids=(POINT,),
        index_name=PROFILE.parent_index_name if parent else PROFILE.index_name,
    )
    build = DocumentTopologyBuild(
        build_id="build",
        job_id="job",
        artifact=reference,
        document_id="doc",
        document_version_id="version",
        chunk_ids=("chunk",),
    )
    return row, manifest, build


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [False, True])
@pytest.mark.filterwarnings("ignore:Payload indexes have no effect:UserWarning")
async def test_frozen_float32_dot_points_roundtrip_exactly_through_real_qdrant(parent):
    vectors = QdrantVectorRepository(QdrantVectorConfig(deployment="embedded"))
    await vectors.connect()
    try:
        async with MemoryObjectStore() as objects:
            expected, manifest, build = await frozen(objects, parent=parent)
            await DerivedVectorProjection(vectors, ImmutableArtifactReader(objects)).publish(
                build, manifest, context=CONTEXT
            )
            actual = await vectors.get_records(
                manifest.index_name, manifest.point_ids, context=CONTEXT
            )
            assert actual == [expected]
            assert "tenant_id" not in actual[0].payload
            assert actual[0].named_vectors == {}
            assert actual[0].sparse_vector is None
            spec = await vectors._queries.require_spec(manifest.index_name, CONTEXT)
            assert spec.distance == VectorDistance.DOT_PRODUCT
    finally:
        await vectors.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [False, True])
@pytest.mark.filterwarnings("ignore:Payload indexes have no effect:UserWarning")
async def test_retired_derived_cleanup_deletes_only_frozen_manifest_points(parent):
    vectors = QdrantVectorRepository(QdrantVectorConfig(deployment="embedded"))
    await vectors.connect()
    try:
        async with MemoryObjectStore() as objects:
            _, manifest, build = await frozen(objects, parent=parent)
            projection = DerivedVectorProjection(vectors, ImmutableArtifactReader(objects))
            await projection.publish(build, manifest, context=CONTEXT)
            kind = "parent_description" if parent else "contextual_chunk"
            record = DerivedArtifactRecord(
                lineage=DerivedArtifactLineage(
                    artifact_id=f"artifact-{kind}",
                    artifact_kind=kind,
                    build_id=build.build_id,
                    input_digest="input-digest",
                    input_document_versions={build.document_id: build.document_version_id},
                    permission_dependencies=(
                        PermissionDependency(
                            resource_kind="source", resource_id="scope", revision="source-r1"
                        ),
                        PermissionDependency(
                            resource_kind="document", resource_id="doc", revision="doc-r1"
                        ),
                    ),
                    metadata=manifest.model_dump(mode="json", exclude={"artifact"}),
                ),
                artifact=manifest.artifact,
            )
            cleaner = RetiredDerivedProjectionCleaner(vectors)
            assert await cleaner.delete(build.build_id, (record,), context=CONTEXT) == 1
            assert not await vectors.index_exists(manifest.index_name, context=CONTEXT)
            assert await cleaner.delete(build.build_id, (record,), context=CONTEXT) == 0
    finally:
        await vectors.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [{"named": True}, {"quantized": False}])
async def test_unsupported_lanes_and_unfrozen_precision_fail_before_any_write(settings):
    vectors = QdrantVectorRepository(QdrantVectorConfig(deployment="embedded"))
    await vectors.connect()
    try:
        async with MemoryObjectStore() as objects:
            _, manifest, build = await frozen(objects, **settings)
            with pytest.raises(ValueError, match="ownership or profile"):
                await DerivedVectorProjection(vectors, ImmutableArtifactReader(objects)).publish(
                    build, manifest, context=CONTEXT
                )
            collections = await vectors._database.raw.get_collections()
            assert collections.collections == []
    finally:
        await vectors.close()


def test_storage_contract_changes_embedding_projection_fingerprint():
    assert PROFILE.storage_revision == "float32-unit-dot-v1"
    assert "storage_revision" in PROFILE.model_dump()


def test_remote_json_float_rendering_matches_at_qdrant_storage_precision():
    expected = VectorIndexRecord(
        id=POINT,
        tenant_id="tenant-1",
        vector=list(canonical_dense_vector((0.6, 0.8))),
        payload={"build_id": "build"},
    )
    transported = expected.model_copy(update={"vector": [0.6, 0.8]})

    assert transported != expected
    assert records_match_at_storage_precision([transported], [expected])
    assert not records_match_at_storage_precision(
        [transported.model_copy(update={"payload": {"build_id": "corrupt"}})],
        [expected],
    )


@pytest.mark.asyncio
async def test_contextual_checkpoints_require_exact_once_coverage_of_every_chunk():
    async with MemoryObjectStore() as objects:
        row, _, build = await frozen(objects)
        for records, owner in (
            ([], build),
            ([row, row], build),
            ([row], build.model_copy(update={"chunk_ids": ("chunk", "missing")})),
        ):
            with pytest.raises(ValueError, match="coverage"):
                validate_derived_coverage(records, owner, "contextual")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"cited_chunk_ids": []},
        {"cited_chunk_ids": ["unknown"]},
        {"parent_key": ""},
        {"level": "section"},
        {"chunk_ids": []},
    ],
)
async def test_parent_checkpoints_reject_missing_document_coverage_and_bad_citations(changes):
    async with MemoryObjectStore() as objects:
        row, _, build = await frozen(objects, parent=True)
        changed = row.model_copy(update={"payload": {**row.payload, **changes}})
        with pytest.raises(ValueError):
            validate_derived_coverage([changed], build, "parent_description")


@pytest.mark.asyncio
async def test_parent_keys_are_unique_and_document_summary_covers_all_source_inputs():
    async with MemoryObjectStore() as objects:
        row, _, build = await frozen(objects, parent=True)
        with pytest.raises(ValueError, match="distinct parent"):
            validate_derived_coverage([row, row], build, "parent_description")
        larger = build.model_copy(update={"chunk_ids": ("chunk", "extra")})
        section = row.model_copy(
            update={
                "payload": {
                    **row.payload,
                    "parent_key": "section",
                    "level": "section",
                    "chunk_ids": ["extra"],
                    "cited_chunk_ids": ["extra"],
                }
            }
        )
        with pytest.raises(ValueError, match="every input"):
            validate_derived_coverage([row, section], larger, "parent_description")
