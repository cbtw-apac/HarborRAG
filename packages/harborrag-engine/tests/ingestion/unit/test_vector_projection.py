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


def test_vector_projection_carries_source_descriptors() -> None:
    """A caller narrows a result set by time, state, owner, and taxonomy.

    Every one of these already reaches the chunk; the payload used to drop them,
    so a live point could not be filtered or sorted on anything but identity.
    """

    result = make_service(
        make_profile(name="jira", strategy="jira", target=100, maximum=120),
        configuration_version="3",
    ).chunk(
        make_request(
            make_document(
                [
                    DocumentElement(
                        "comment-1",
                        "paragraph",
                        "Reviewed the rollout plan.",
                        {
                            "field": "comment",
                            "comment_id": "1001",
                            "author": "Ada",
                            "created_at": "2026-03-04T09:00:00+00:00",
                            "updated_at": "2026-03-05T09:00:00+00:00",
                        },
                    )
                ],
                source="jira",
                record_id="AMAST-2",
                extra={
                    "issue_key": "AMAST-2",
                    "status": "In Progress",
                    "status_category": "In Progress",
                    "issue_type": "Bug",
                    "priority": "High",
                    "assignee": "Grace",
                    "reporter": "Alan",
                    "creator": "Alan",
                    "project_key": "AMAST",
                    "project_name": "Harbor Ingestion",
                    "labels": ["ingestion", "urgent"],
                    "components": ["worker"],
                    "source_created_at": "2026-01-01T00:00:00+00:00",
                    "source_updated_at": "2026-03-09T00:00:00+00:00",
                },
            )
        )
    )

    payload = (
        VectorProjectionBuilder()
        .build(
            VectorProjectionInput(
                chunks=result.chunks,
                representations=representation_set(result.chunks),
                chunk_artifacts=chunk_set(result.chunks),
            )
        )
        .evidence_records[0]
        .payload
    )

    assert payload.status == "In Progress"
    assert payload.status_category == "In Progress"
    assert payload.item_type == "Bug"
    assert payload.priority == "High"
    assert payload.assignee == "Grace"
    assert payload.reporter == "Alan"
    assert payload.creator == "Alan"
    assert payload.project_key == "AMAST"
    assert payload.project_name == "Harbor Ingestion"
    assert payload.labels == ("ingestion", "urgent")
    assert payload.components == ("worker",)
    # The comment's own dates, not the issue's: it is the evidence being cited.
    assert payload.author == "Ada"
    assert payload.created_at == "2026-03-04T09:00:00+00:00"
    assert payload.updated_at == "2026-03-05T09:00:00+00:00"


def test_vector_projection_falls_back_to_document_timestamps() -> None:
    result = make_service(
        make_profile(name="jira", strategy="jira", target=100, maximum=120),
        configuration_version="3",
    ).chunk(
        make_request(
            make_document(
                [DocumentElement("summary", "paragraph", "A bug", {"field": "summary"})],
                source="jira",
                record_id="AMAST-2",
                extra={
                    "issue_key": "AMAST-2",
                    "source_created_at": "2026-01-01T00:00:00+00:00",
                    "source_updated_at": "2026-03-09T00:00:00+00:00",
                },
            )
        )
    )

    payload = (
        VectorProjectionBuilder()
        .build(
            VectorProjectionInput(
                chunks=result.chunks,
                representations=representation_set(result.chunks),
                chunk_artifacts=chunk_set(result.chunks),
            )
        )
        .evidence_records[0]
        .payload
    )

    assert payload.created_at == "2026-01-01T00:00:00+00:00"
    assert payload.updated_at == "2026-03-09T00:00:00+00:00"
    assert payload.labels == ()


def test_every_evidence_point_of_an_issue_carries_its_typed_fields() -> None:
    """A filter on a custom field must reach the comments too, not only the summary.

    The fields describe the issue, so each chunk of it inherits them, and they
    keep their type: a number stays a number so a range filter can match it.
    """

    fields = {
        "skill_set": "Data Engineering",
        "years_of_experience": 3.0,
        "restricted_to": ["Aidan NELL", "Martin PAPY"],
        "pd_consent": False,
    }
    result = make_service(
        make_profile(name="jira", strategy="jira", target=100, maximum=120),
        configuration_version="3",
    ).chunk(
        make_request(
            make_document(
                [
                    DocumentElement("summary", "paragraph", "A candidate", {"field": "summary"}),
                    DocumentElement(
                        "comment-1",
                        "paragraph",
                        "Passed the technical round.",
                        {"field": "comment", "comment_id": "1001"},
                    ),
                ],
                source="jira",
                record_id="CPM-1",
                extra={"issue_key": "CPM-1", "fields": fields},
            )
        )
    )

    records = (
        VectorProjectionBuilder()
        .build(
            VectorProjectionInput(
                chunks=result.chunks,
                representations=representation_set(result.chunks),
                chunk_artifacts=chunk_set(result.chunks),
            )
        )
        .evidence_records
    )

    assert len(records) == 2
    for record in records:
        assert record.payload.fields == {
            "skill_set": "Data Engineering",
            "years_of_experience": 3.0,
            "restricted_to": ("Aidan NELL", "Martin PAPY"),
            "pd_consent": False,
        }
        stored = record.payload.model_dump(mode="json", exclude_none=True)["fields"]
        assert stored["restricted_to"] == ["Aidan NELL", "Martin PAPY"]
        assert stored["pd_consent"] is False


def test_a_document_without_typed_fields_stores_no_fields_key() -> None:
    result = make_service(
        make_profile(name="jira", strategy="jira", target=100, maximum=120),
        configuration_version="3",
    ).chunk(
        make_request(
            make_document(
                [DocumentElement("summary", "paragraph", "A bug", {"field": "summary"})],
                source="jira",
                record_id="AMAST-2",
                extra={"issue_key": "AMAST-2"},
            )
        )
    )
    payload = (
        VectorProjectionBuilder()
        .build(
            VectorProjectionInput(
                chunks=result.chunks,
                representations=representation_set(result.chunks),
                chunk_artifacts=chunk_set(result.chunks),
            )
        )
        .evidence_records[0]
        .payload
    )
    assert payload.fields is None
    assert "fields" not in payload.model_dump(mode="json", exclude_none=True)


@pytest.mark.parametrize(
    "fields",
    (
        {"Skill Set": "Data"},  # not a normalized key: a dotted filter path would be ambiguous
        {"skill_set": "  "},
        {"skills": ("java", "")},
        {"rate": float("nan")},
    ),
)
def test_payload_fields_must_be_normalized_and_non_empty(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VectorPayload.model_validate({**_minimal_payload(), "fields": fields})


def _minimal_payload() -> dict[str, object]:
    result = make_service(
        make_profile(name="jira", strategy="jira", target=100, maximum=120),
        configuration_version="3",
    ).chunk(
        make_request(
            make_document(
                [DocumentElement("summary", "paragraph", "A bug", {"field": "summary"})],
                source="jira",
                record_id="AMAST-2",
                extra={"issue_key": "AMAST-2"},
            )
        )
    )
    return (
        VectorProjectionBuilder()
        .build(
            VectorProjectionInput(
                chunks=result.chunks,
                representations=representation_set(result.chunks),
                chunk_artifacts=chunk_set(result.chunks),
            )
        )
        .evidence_records[0]
        .payload.model_dump()
    )


def test_an_attachment_chunk_names_the_item_it_is_attached_to() -> None:
    """A CV is its own document, chunked by a strategy that copies no provenance.

    Its parent's item id is what lets a filter on the issue's fields reach it,
    so every one of its evidence points carries that id.
    """

    result = make_service(make_profile(target=100, maximum=120)).chunk(
        make_request(
            make_document(
                [DocumentElement("p1", "paragraph", "Data Engineer with five years of Spark.")],
                extra={
                    "binding_kind": "ATTACHMENT",
                    "parent_source_item_id": "jira://CPM/CPM-116680",
                },
            )
        )
    )

    records = (
        VectorProjectionBuilder()
        .build(
            VectorProjectionInput(
                chunks=result.chunks,
                representations=representation_set(result.chunks),
                chunk_artifacts=chunk_set(result.chunks),
            )
        )
        .evidence_records
    )

    assert records
    assert {record.payload.parent_source_item_id for record in records} == {"jira://CPM/CPM-116680"}


def test_each_point_carries_its_graph_node_keys_from_the_same_version() -> None:
    """A vector hit joins the graph on stored keys, not on a re-derived source id."""

    from harborrag_core.chunking import RelationType
    from harborrag_core.ingestion import KnowledgeNodeKind
    from harborrag_engine.ingestion import GraphProjectionBuilder

    document = make_document(
        [DocumentElement("p1", "paragraph", "The worker timeout is 30 seconds for AMAST-2.")],
        source="jira",
        record_id="AMAST-2",
        extra={"issue_key": "AMAST-2", "project_id": "10000", "project_key": "AMAST"},
    )
    result = make_service(
        make_profile(name="jira", strategy="jira", target=100, maximum=120),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(make_request(document))
    graph = GraphProjectionBuilder().build_structural(
        document=document,
        chunks=result.chunks,
        graph_projection_version="graph-v2",
    )

    projection = VectorProjectionBuilder().build(
        VectorProjectionInput(
            chunks=result.chunks,
            representations=representation_set(result.chunks),
            chunk_artifacts=chunk_set(result.chunks),
            graph=graph,
        )
    )

    chunk_nodes = {
        node.logical_id: node.node_key
        for node in graph.nodes
        if node.node_kind == KnowledgeNodeKind.CHUNK
    }
    (has_version,) = (r for r in graph.relations if r.relation_type is RelationType.HAS_VERSION)
    for record in projection.evidence_records:
        assert record.payload.graph_chunk_node_key == chunk_nodes[record.payload.chunk_id]
        assert record.payload.graph_source_node_key == has_version.source_node_key


def test_points_built_without_a_graph_carry_no_graph_keys() -> None:
    result = make_service(
        make_profile(name="jira", strategy="jira", target=100, maximum=120),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(
        make_request(
            make_document(
                [DocumentElement("p1", "paragraph", "Plain text.")],
                source="jira",
                record_id="AMAST-3",
                extra={"issue_key": "AMAST-3", "project_id": "10000"},
            )
        )
    )

    projection = VectorProjectionBuilder().build(
        VectorProjectionInput(
            chunks=result.chunks,
            representations=representation_set(result.chunks),
            chunk_artifacts=chunk_set(result.chunks),
        )
    )

    payload = projection.evidence_records[0].payload
    assert payload.graph_chunk_node_key is None
    assert payload.graph_source_node_key is None
