from __future__ import annotations

import pytest

from harborrag_engine.ingestion.indexing import (
    GraphIndexService,
    GraphIndexValidationError,
    GraphMutationPlanner,
)

from .indexing_helpers import (
    FakeGraphRepository,
    make_index_request,
    make_manifest,
    make_record,
    make_reference,
)


def make_graph_request():
    references = (
        make_reference(
            "logical-1",
            "revision-1",
            "hash-1",
            ordinal=0,
            body_uri="object://chunk/revision-1",
        ),
        make_reference("logical-2", "revision-2", "hash-2", ordinal=1),
        make_reference("logical-3", "revision-3", "hash-3", ordinal=2),
    )
    manifest = make_manifest(references, artifact_revision_id="artifact-revision-1")
    records = (
        make_record(
            references[0],
            artifact_revision_id="artifact-revision-1",
            content="A preview that is deliberately longer than the capsule limit",
            structural_path=("Guide",),
        ),
        make_record(
            references[1],
            artifact_revision_id="artifact-revision-1",
            structural_path=("Guide", "Setup"),
        ),
        make_record(
            references[2],
            artifact_revision_id="artifact-revision-1",
            structural_path=(),
        ),
    )
    return make_index_request(proposed=manifest, records=records)


def test_universal_graph_contains_artifact_revision_sections_chunks_and_order() -> None:
    plan = GraphMutationPlanner().plan(make_graph_request())

    labels = [node.labels for node in plan.nodes]
    relationships = [edge.relationship_type for edge in plan.edges]
    assert sum("Artifact" in value for value in labels) == 1
    assert sum("Revision" in value for value in labels) == 1
    assert sum("Section" in value for value in labels) == 2
    assert sum("Chunk" in value for value in labels) == 3
    assert relationships.count("HAS_REVISION") == 1
    assert relationships.count("HAS_SECTION") >= 1
    assert relationships.count("HAS_SUBSECTION") == 1
    assert relationships.count("HAS_CHUNK") == 3
    assert relationships.count("NEXT_CHUNK") == 2
    assert relationships.count("PREVIOUS_CHUNK") == 2


def test_graph_capsules_are_bounded_and_do_not_store_canonical_content() -> None:
    plan = GraphMutationPlanner().plan(make_graph_request())
    chunk_nodes = [node for node in plan.nodes if "Chunk" in node.labels]

    assert len(chunk_nodes) == 3
    assert all(len(node.properties["preview"]) <= 32 for node in chunk_nodes)
    assert all("content" not in node.properties for node in chunk_nodes)
    first = next(
        node for node in chunk_nodes if node.properties["chunk_revision_id"] == "revision-1"
    )
    assert first.properties["preview_truncated"] is True
    assert first.properties["content_reference"] == "object://chunk/revision-1"


@pytest.mark.asyncio
async def test_repeated_graph_indexing_is_idempotent() -> None:
    request = make_graph_request()
    repository = FakeGraphRepository()
    service = GraphIndexService(
        graph_repository=repository,  # type: ignore[arg-type]
    )

    first = await service.stage(request)
    second = await service.stage(request)

    assert [node.id for node in first.plan.nodes] == [node.id for node in second.plan.nodes]
    assert [edge.id for edge in first.plan.edges] == [edge.id for edge in second.plan.edges]
    assert len(repository.nodes) == len(first.plan.nodes)
    assert len(repository.edges) == len(first.plan.edges)
    assert first.validation.valid and second.validation.valid


@pytest.mark.asyncio
async def test_graph_validation_reports_missing_edges() -> None:
    service = GraphIndexService(
        graph_repository=FakeGraphRepository(drop_edge=True),  # type: ignore[arg-type]
    )

    with pytest.raises(GraphIndexValidationError, match="edges are missing"):
        await service.stage(make_graph_request())


def test_changed_and_removed_chunks_record_retired_graph_nodes() -> None:
    active_references = (
        make_reference("logical-change", "revision-old", "hash-old", ordinal=0),
        make_reference("logical-remove", "revision-remove", "hash-remove", ordinal=1),
    )
    proposed_references = (make_reference("logical-change", "revision-new", "hash-new", ordinal=0),)
    active = make_manifest(active_references, artifact_revision_id="artifact-revision-1")
    proposed = make_manifest(proposed_references, artifact_revision_id="artifact-revision-2")
    record = make_record(proposed_references[0], artifact_revision_id="artifact-revision-2")

    plan = GraphMutationPlanner().plan(
        make_index_request(
            proposed=proposed,
            records=(record,),
            active=active,
            active_fingerprint="old-embedding-config",
            active_generation_id="generation-1",
        )
    )

    assert len(plan.retired_node_ids) == 2
