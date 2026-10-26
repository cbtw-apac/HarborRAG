"""Admitted exact point scopes must precede generated-vector ranking."""

from __future__ import annotations

import pytest
from topology_retrieval_support import TopologyVectors

from harborrag_core.indexing import (
    VectorFilter,
    VectorFilterCondition,
    VectorSearchQuery,
    VectorSearchResult,
)
from harborrag_core.ingestion import ArtifactReference
from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology.derived import ContextualIndexProfile
from harborrag_core.topology.permissions import (
    DerivedArtifactLineage,
    DerivedArtifactRecord,
    PermissionDependency,
)
from harborrag_runtime.retrieval.contextual import ContextualEvidenceSearch

PROFILE = ContextualIndexProfile(model="existing", dimension=3, deployment_revision="pinned")
CONTEXT = StorageOperationContext.system("tenant-1")


def manifest(*, parent=False):
    return DerivedArtifactRecord(
        artifact=ArtifactReference(
            bucket="artifacts",
            key="frozen.json",
            sha256="a" * 64,
            byte_size=1,
            media_type="application/json",
        ),
        lineage=DerivedArtifactLineage(
            artifact_id="artifact-1",
            artifact_kind="parent_description" if parent else "contextual_chunk",
            build_id="build-2",
            input_document_versions={"document-2": "version-2"},
            input_digest="snapshot",
            permission_dependencies=(
                PermissionDependency(
                    resource_kind="source", resource_id="scope-1", revision="acl-1"
                ),
                PermissionDependency(
                    resource_kind="document", resource_id="document-2", revision="acl-2"
                ),
            ),
            metadata={
                "index_name": PROFILE.parent_index_name if parent else PROFILE.index_name,
                "embedding_profile": PROFILE.parent_fingerprint if parent else PROFILE.fingerprint,
                "dimension": PROFILE.dimension,
                "point_ids": ["generated-point"],
            },
        ),
    )


class Authority:
    def __init__(self, *, parent=False):
        self.records = (manifest(parent=parent),)
        self.calls = []

    async def active_derivations(self, tenant_id, *, access=None, artifact_kind=None, limit=100):
        self.calls.append((tenant_id, access))
        return self.records


class Vectors(TopologyVectors):
    def __init__(self, *, parent=False):
        super().__init__()
        self.search_calls = []
        payload = {
            "projection_point_id": "generated-point",
            "record_kind": "contextual",
            "chunk_id": "chunk-2",
            "document_id": "document-2",
            "document_version_id": "version-2",
            "build_id": "build-2",
            "embedding_profile": PROFILE.fingerprint,
        }
        if parent:
            payload.update(
                embedding_profile=PROFILE.parent_fingerprint,
                record_kind="parent_description",
                parent_key="document-parent",
                level="document",
                chunk_ids=["chunk-2"],
                cited_chunk_ids=["chunk-2"],
                description="Generated navigation",
            )
        self.hit = VectorSearchResult(
            id="generated-point", score=0.8, raw_score=0.8, payload=payload
        )

    async def search(self, query, *, context):
        self.search_calls.append((query, context))
        return [self.hit]


def query(**kwargs):
    return VectorSearchQuery(index_name="evidence", vector=[1, 0, 0], **kwargs)


@pytest.mark.asyncio
async def test_exact_manifest_points_are_filtered_before_search_and_mapped_back_to_raw():
    authority, vectors = Authority(), Vectors()
    result = await ContextualEvidenceSearch(authority, vectors, PROFILE).search(
        query(), context=CONTEXT
    )
    assert authority.calls == [("tenant-1", CONTEXT.access)]
    scoped, context = vectors.search_calls[0]
    assert scoped.index_name == PROFILE.index_name
    assert context == CONTEXT
    assert scoped.filters.must[0].field == "projection_point_id"
    assert scoped.filters.must[0].value == ["generated-point"]
    assert result[0][0].payload["content"] == "Beta controls retries."
    assert result[0][1].derived_artifact_ids == ("artifact-1",)
    assert result[0][1].build_ids == ("build-2",)
    assert vectors.get_calls[0][0] == "evidence"


@pytest.mark.asyncio
async def test_disabled_or_unknown_lineage_does_not_search_generated_vectors():
    authority, vectors = Authority(), Vectors()
    authority.records = ()
    assert (
        await ContextualEvidenceSearch(authority, vectors, PROFILE).search(query(), context=CONTEXT)
        == ()
    )
    assert vectors.search_calls == vectors.get_calls == []


@pytest.mark.asyncio
async def test_changed_embedding_profile_does_not_query_an_incompatible_index():
    vectors = Vectors()
    changed = PROFILE.model_copy(update={"deployment_revision": "new"})
    assert (
        await ContextualEvidenceSearch(Authority(), vectors, changed).search(
            query(), context=CONTEXT
        )
        == ()
    )
    assert not vectors.search_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("projection_point_id", "other"),
        ("build_id", "retired"),
        ("document_id", "other"),
        ("document_version_id", None),
        ("embedding_profile", "old"),
        ("record_kind", "evidence"),
    ],
)
async def test_projection_payload_cannot_escape_its_exact_authorized_manifest(field, value):
    vectors = Vectors()
    vectors.hit = vectors.hit.model_copy(update={"payload": {**vectors.hit.payload, field: value}})
    assert (
        await ContextualEvidenceSearch(Authority(), vectors, PROFILE).search(
            query(), context=CONTEXT
        )
        == ()
    )
    assert not vectors.get_calls


@pytest.mark.asyncio
async def test_parent_description_is_navigation_only_and_original_evidence_is_preserved():
    result = await ContextualEvidenceSearch(
        Authority(parent=True), Vectors(parent=True), PROFILE
    ).search(query(), context=CONTEXT)
    candidate, support = result[0]
    assert candidate.payload["content"] == "Beta controls retries."
    assert support.navigation_summaries[0]["description"] == "Generated navigation"
    assert support.navigation_summaries[0]["coverage"] == "representative_source_passages"
    assert support.derived_artifact_ids == ("artifact-1",)


@pytest.mark.asyncio
async def test_filtered_requests_do_not_fall_back_to_unfiltered_generated_search():
    authority, vectors = Authority(), Vectors()
    filters = VectorFilter(
        must=[VectorFilterCondition(field="private_metadata", value="restricted")]
    )
    assert (
        await ContextualEvidenceSearch(authority, vectors, PROFILE).search(
            query(filters=filters), context=CONTEXT
        )
        == ()
    )
    assert not authority.calls
    assert not vectors.search_calls


@pytest.mark.asyncio
async def test_raw_payload_with_wrong_source_revision_is_rejected():
    vectors = Vectors()
    vectors.records[1] = vectors.records[1].model_copy(
        update={
            "payload": {**vectors.records[1].payload, "document_version_id": "old"},
        }
    )
    assert (
        await ContextualEvidenceSearch(Authority(), vectors, PROFILE).search(
            query(), context=CONTEXT
        )
        == ()
    )
