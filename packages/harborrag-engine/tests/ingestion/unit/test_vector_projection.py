from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_core.chunking import ChunkRecord
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import (
    ArtifactReference,
    ChunkIndexEntry,
    ChunkRepresentation,
    ChunkSetArtifacts,
    RepresentationSet,
    SparseEncoderProfile,
    VectorPayload,
)
from harborrag_engine.ingestion import (
    BM25SparseEncoder,
    VectorProjectionBuilder,
    VectorProjectionInput,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service


def chunk_set(chunks: tuple[ChunkRecord, ...]) -> ChunkSetArtifacts:
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
            key="chunks/document-1/version-1.jsonl",
            sha256="a" * 64,
            byte_size=len(entries) * 1000,
            media_type="application/x-ndjson",
        ),
        index=ArtifactReference(
            bucket="harborrag-artifacts",
            key="chunks/document-1/version-1.idx",
            sha256="b" * 64,
            byte_size=200,
            media_type="application/x-ndjson",
        ),
        entries=entries,
    )


def representation_set(chunks: tuple[ChunkRecord, ...]) -> RepresentationSet:
    sparse_encoder = BM25SparseEncoder(SparseEncoderProfile(profile_id="bm25-v1"))
    return RepresentationSet(
        document_id=chunks[0].document_id,
        document_version_id=chunks[0].document_version_id,
        dense_profile_id="dense-v1",
        sparse_profile_id=sparse_encoder.profile.profile_id,
        dense_dimension=3,
        records=tuple(
            ChunkRepresentation(
                chunk_id=str(chunk.chunk_id),
                dense_vector=[float(index), 1.0, 0.5],
                sparse_vector=sparse_encoder.encode(chunk.search_text).vector,
            )
            for index, chunk in enumerate(chunks, start=1)
        ),
    )


def test_vector_projection_writes_only_evidence_content() -> None:
    result = make_service(
        make_profile(name="jira", strategy="jira", target=100, maximum=120),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(
        make_request(
            make_document(
                [
                    DocumentElement(
                        "p1",
                        "paragraph",
                        "The worker timeout is 30 seconds for AMAST-2.",
                    )
                ],
                source="jira",
                record_id="AMAST-2",
                extra={"issue_key": "AMAST-2", "project_id": "10000"},
            )
        )
    )
    builder = VectorProjectionBuilder()

    projection = builder.build(
        VectorProjectionInput(
            chunks=result.chunks,
            representations=representation_set(result.chunks),
            chunk_artifacts=chunk_set(result.chunks),
        )
    )

    assert len(projection.evidence_records) == 1
    evidence_payload = projection.evidence_records[0].payload
    assert evidence_payload.record_kind.value == "evidence"
    assert evidence_payload.document_version_id == "document-version:1"
    assert evidence_payload.issue_key == "AMAST-2"
    assert evidence_payload.content == "The worker timeout is 30 seconds for AMAST-2."
    assert evidence_payload.document_title == "HarborRAG"
    assert evidence_payload.source_item_id
    assert evidence_payload.document_kind.value == "jira_issue"
    assert evidence_payload.token_count > 0
    assert evidence_payload.content_hash
    serialized = evidence_payload.model_dump(mode="json", exclude_none=True)
    assert "preview" not in serialized
    assert "content_reference" not in serialized
    assert "is_active" not in serialized
    assert "workflow_id" not in serialized
    assert "exact_identifiers" not in serialized
    assert projection.evidence_records[0].sparse_vector is not None
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        VectorPayload.model_validate({**serialized, "raw_parser_metadata": {"x": 1}})


def test_vector_projection_requires_every_dense_vector() -> None:
    result = make_service(
        make_profile(target=100, maximum=120),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(make_request(make_document([DocumentElement("p1", "paragraph", "Evidence")])))
    builder = VectorProjectionBuilder()

    incomplete = representation_set(result.chunks).model_copy(update={"records": ()})
    with pytest.raises(ValueError, match="representation is missing"):
        builder.build(
            VectorProjectionInput(
                chunks=result.chunks,
                representations=incomplete,
                chunk_artifacts=chunk_set(result.chunks),
            )
        )
