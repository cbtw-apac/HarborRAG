from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_engine.ingestion.indexing import (
    GraphIndexService,
    IndexingService,
    IndexingStatus,
    VectorIndexService,
)

from .indexing_helpers import (
    CharacterCounter,
    FakeEmbedClient,
    FakeGraphRepository,
    FakeVectorRepository,
    make_index_request,
    make_manifest,
    make_record,
    make_reference,
)


def make_request():
    references = (
        make_reference("logical-1", "revision-1", "hash-1", ordinal=0),
        make_reference("logical-2", "revision-2", "hash-2", ordinal=1),
    )
    manifest = make_manifest(references, artifact_revision_id="artifact-revision-1")
    records = tuple(
        make_record(reference, artifact_revision_id="artifact-revision-1")
        for reference in references
    )
    return make_index_request(proposed=manifest, records=records)


def make_service(embed_client, vector_repository, graph_repository) -> IndexingService:
    return IndexingService(
        vector_service=VectorIndexService(
            embed_client=embed_client,
            vector_repository=vector_repository,  # type: ignore[arg-type]
            token_counter=CharacterCounter(),
        ),
        graph_service=GraphIndexService(
            graph_repository=graph_repository,  # type: ignore[arg-type]
        ),
    )


class OperatingSystemFailureVectorRepository(FakeVectorRepository):
    async def upsert(self, collection, points, *, context) -> None:
        del collection, points, context
        raise OSError("vector transport failed")


@pytest.mark.asyncio
async def test_combined_indexing_returns_success_and_diagnostics() -> None:
    result = await make_service(
        FakeEmbedClient(),
        FakeVectorRepository(),
        FakeGraphRepository(),
    ).index(make_request())

    assert result.status is IndexingStatus.SUCCEEDED
    assert result.vector_valid and result.graph_valid
    assert result.validation_errors == ()
    assert result.artifact_id == "artifact-1"
    assert result.artifact_revision_id == "artifact-revision-1"
    assert result.generation_id == "generation-2"
    assert result.diagnostics.new_chunks == 2
    assert result.diagnostics.embedded_chunks == 2
    assert result.diagnostics.vector_upserts == 2
    assert result.diagnostics.graph_nodes > 0
    assert result.diagnostics.graph_edges > 0
    assert len(result.activation.activate_vector_ids) == 2
    assert result.activation.previous_generation_id is None


@pytest.mark.asyncio
async def test_combined_indexing_reports_vector_success_and_graph_failure() -> None:
    result = await make_service(
        FakeEmbedClient(),
        FakeVectorRepository(),
        FakeGraphRepository(fail=True),
    ).index(make_request())

    assert result.status is IndexingStatus.PARTIAL
    assert result.vector_valid is True
    assert result.graph_valid is False
    assert result.validation_errors[0].startswith("graph: RuntimeError")


@pytest.mark.asyncio
async def test_combined_indexing_reports_graph_success_and_vector_failure() -> None:
    result = await make_service(
        FakeEmbedClient(),
        FakeVectorRepository(fail=True),
        FakeGraphRepository(),
    ).index(make_request())

    assert result.status is IndexingStatus.PARTIAL
    assert result.vector_valid is False
    assert result.graph_valid is True
    assert result.validation_errors[0].startswith("vector: RuntimeError")


@pytest.mark.asyncio
async def test_combined_indexing_contains_non_runtime_provider_failures() -> None:
    result = await make_service(
        FakeEmbedClient(),
        OperatingSystemFailureVectorRepository(),
        FakeGraphRepository(),
    ).index(make_request())

    assert result.status is IndexingStatus.PARTIAL
    assert result.graph_valid is True
    assert result.validation_errors[0].startswith("vector: OSError")


@pytest.mark.asyncio
async def test_repeated_combined_indexing_does_not_create_duplicates() -> None:
    vector_repository = FakeVectorRepository()
    graph_repository = FakeGraphRepository()
    service = make_service(FakeEmbedClient(), vector_repository, graph_repository)
    request = make_request()

    first = await service.index(request)
    vector_count = len(vector_repository.points)
    node_count = len(graph_repository.nodes)
    edge_count = len(graph_repository.edges)
    second = await service.index(request)

    assert first.status is IndexingStatus.SUCCEEDED
    assert second.status is IndexingStatus.SUCCEEDED
    assert len(vector_repository.points) == vector_count
    assert len(graph_repository.nodes) == node_count
    assert len(graph_repository.edges) == edge_count


@pytest.mark.asyncio
async def test_partial_retry_resumes_only_the_failed_provider_side() -> None:
    embed_client = FakeEmbedClient()
    vector_repository = FakeVectorRepository()
    request = make_request()
    first = await make_service(
        embed_client,
        vector_repository,
        FakeGraphRepository(fail=True),
    ).index(request)
    embed_requests = len(embed_client.requests)
    vector_upserts = vector_repository.upsert_calls

    second = await make_service(
        embed_client,
        vector_repository,
        FakeGraphRepository(),
    ).index(replace(request, resume_result=first))

    assert first.status is IndexingStatus.PARTIAL
    assert first.failures[0].component == "graph"
    assert first.failures[0].retryable is True
    assert second.status is IndexingStatus.SUCCEEDED
    assert second.failures == ()
    assert len(embed_client.requests) == embed_requests
    assert vector_repository.upsert_calls == vector_upserts
