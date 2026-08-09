from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from harborrag_core.chunking import ChunkRecord
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import (
    ArtifactReference,
    ChunkIndexEntry,
    ChunkRepresentation,
    ChunkSetArtifacts,
    GraphProjectionVerification,
    ProjectionManifest,
    RepresentationSet,
    SparseEncoderProfile,
    VectorProjectionVerification,
)
from harborrag_engine.ingestion import (
    BM25SparseEncoder,
    GraphProjectionBatch,
    GraphProjectionBuilder,
    GraphProjectionInput,
    ProjectionManifestBuilder,
    ProjectionManifestInput,
    ProjectionVerificationInput,
    ProjectionVerifier,
    VectorProjectionBatch,
    VectorProjectionBuilder,
    VectorProjectionInput,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service


@dataclass(frozen=True, slots=True)
class VerificationFixture:
    manifest: ProjectionManifest
    chunks: tuple[ChunkRecord, ...]
    vectors: VectorProjectionBatch
    graph: GraphProjectionBatch
    vector_result: VectorProjectionVerification
    graph_result: GraphProjectionVerification


def test_cross_projection_verification_accepts_one_consistent_document_version() -> None:
    values = projection_values()

    result = ProjectionVerifier().verify(
        ProjectionVerificationInput(
            manifest=values.manifest,
            chunks=values.chunks,
            vectors=values.vectors,
            graph=values.graph,
            vector_result=values.vector_result,
            graph_result=values.graph_result,
        )
    )

    assert result.valid is True
    assert result.cross_projection_errors == ()


def test_cross_projection_verification_rejects_payload_version_drift() -> None:
    values = projection_values()
    vectors = values.vectors
    evidence = vectors.evidence_records[0]
    invalid_evidence = evidence.model_copy(
        update={
            "payload": evidence.payload.model_copy(update={"document_version_id": "stale-version"})
        }
    )

    result = ProjectionVerifier().verify(
        ProjectionVerificationInput(
            manifest=values.manifest,
            chunks=values.chunks,
            vectors=VectorProjectionBatch(
                evidence_records=(invalid_evidence, *vectors.evidence_records[1:]),
                manifest=vectors.manifest.model_copy(
                    update={
                        "evidence_point_ids": tuple(
                            record.point_id
                            for record in (invalid_evidence, *vectors.evidence_records[1:])
                        )
                    }
                ),
            ),
            graph=values.graph,
            vector_result=values.vector_result,
            graph_result=values.graph_result,
        )
    )

    assert result.valid is False
    assert "vector payload document-version ID mismatch" in (result.cross_projection_errors)


def test_cross_projection_verification_rejects_missing_canonical_table() -> None:
    values = projection_values()

    result = ProjectionVerifier().verify(
        ProjectionVerificationInput(
            manifest=values.manifest,
            chunks=values.chunks,
            vectors=values.vectors,
            graph=values.graph,
            vector_result=values.vector_result,
            graph_result=values.graph_result,
            canonical_table_ids=("canonical-table-1",),
        )
    )

    assert result.valid is False
    assert "table chunk references do not match canonical tables" in (
        result.cross_projection_errors
    )


def projection_values() -> VerificationFixture:
    document = make_document(
        [
            DocumentElement("h1", "heading", "Operations", {"level": 1}),
            DocumentElement("p1", "paragraph", "Worker timeout is 30 seconds."),
        ]
    )
    chunking = make_service(
        make_profile(target=80, maximum=100),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(make_request(document))
    chunk_artifacts = artifacts(chunking.chunks)
    sparse_encoder = BM25SparseEncoder(SparseEncoderProfile(profile_id="bm25-v1"))
    representation_records = []
    for chunk in chunking.chunks:
        sparse = sparse_encoder.encode(chunk.search_text)
        representation_records.append(
            ChunkRepresentation(
                chunk_id=str(chunk.chunk_id),
                dense_vector=[0.1, 0.2, 0.3],
                sparse_vector=sparse.vector,
            )
        )
    representations = RepresentationSet(
        document_id=chunking.document_id,
        document_version_id=chunking.document_version_id,
        dense_profile_id="dense-v1",
        sparse_profile_id="bm25-v1",
        dense_dimension=3,
        records=tuple(representation_records),
    )
    vectors = VectorProjectionBuilder().build(
        VectorProjectionInput(
            chunks=chunking.chunks,
            representations=representations,
            chunk_artifacts=chunk_artifacts,
        )
    )
    graph = GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunking.chunks,
            resolved_targets={},
            graph_projection_version="graph-v1",
        )
    )
    manifest = ProjectionManifestBuilder().build(
        ProjectionManifestInput(
            document_id=chunking.document_id,
            document_version_id=chunking.document_version_id,
            chunks=chunking.chunks,
            vectors=vectors,
            graph=graph,
        )
    )
    return VerificationFixture(
        manifest=manifest,
        chunks=chunking.chunks,
        vectors=vectors,
        graph=graph,
        vector_result=VectorProjectionVerification(
            valid=True,
            expected_evidence_count=len(vectors.evidence_records),
            actual_evidence_count=len(vectors.evidence_records),
        ),
        graph_result=GraphProjectionVerification(
            valid=True,
            expected_node_count=len(graph.nodes),
            actual_node_count=len(graph.nodes),
            expected_relation_count=len(graph.relations),
            actual_relation_count=len(graph.relations),
        ),
    )


def artifacts(chunks: Sequence[ChunkRecord]) -> ChunkSetArtifacts:
    entries = tuple(
        ChunkIndexEntry(
            chunk_id=str(chunk.chunk_id),
            byte_offset=index * 1000,
            byte_length=900,
        )
        for index, chunk in enumerate(chunks)
    )
    return ChunkSetArtifacts(
        chunks=ArtifactReference(
            bucket="harborrag-artifacts",
            key="chunks/doc-1/revision-1.jsonl",
            sha256="a" * 64,
            byte_size=len(entries) * 1000,
            media_type="application/x-ndjson",
        ),
        index=ArtifactReference(
            bucket="harborrag-artifacts",
            key="chunks/doc-1/revision-1.idx",
            sha256="b" * 64,
            byte_size=200,
            media_type="application/x-ndjson",
        ),
        entries=entries,
    )
