from __future__ import annotations

import pytest

from harborrag_adapters.repositories.vector import HarborVectorDBClient
from harborrag_engine.ingestion.indexing import VectorIndexService

from ..unit.indexing_helpers import (
    CharacterCounter,
    FakeEmbedClient,
    make_config,
    make_index_request,
    make_manifest,
    make_record,
    make_reference,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_deps]


@pytest.mark.asyncio
async def test_vector_indexing_round_trips_through_embedded_qdrant() -> None:
    """Stage deterministic inactive points through the real Qdrant adapter."""

    pytest.importorskip("qdrant_client")
    references = (
        make_reference("logical-1", "revision-1", "hash-1", ordinal=0),
        make_reference("logical-2", "revision-2", "hash-2", ordinal=1),
    )
    manifest = make_manifest(references, artifact_revision_id="artifact-revision-1")
    records = tuple(
        make_record(reference, artifact_revision_id="artifact-revision-1")
        for reference in references
    )
    config = make_config(vector_collection="qdrant_indexing_integration")
    request = make_index_request(proposed=manifest, records=records, config=config)
    repository = HarborVectorDBClient.default().create(
        backend="qdrant",
        instance_name="engine-indexing-integration",
        options={"deployment": "embedded"},
    )
    service = VectorIndexService(
        embed_client=FakeEmbedClient(),
        vector_repository=repository,
        token_counter=CharacterCounter(),
    )

    async with repository:
        try:
            first = await service.stage(request)
            second = await service.stage(request)
            expected_ids = [point.id for point in first.plan.points]
            loaded = await repository.get(
                config.vector_collection,
                expected_ids,
                context=request.context,
            )
            scanned = await repository.scan(
                config.vector_collection,
                limit=10,
                cursor=None,
                context=request.context,
            )

            assert first.validation.valid and second.validation.valid
            assert expected_ids == [point.id for point in second.plan.points]
            assert {point.id for point in loaded} == set(expected_ids)
            assert {point.id for point in scanned.points} == set(expected_ids)
            assert all(point.payload["index_state"] == "staged" for point in loaded)
            assert all(point.payload["is_active"] is False for point in loaded)
        finally:
            if await repository.collection_exists(
                config.vector_collection,
                context=request.context,
            ):
                await repository.delete_collection(
                    config.vector_collection,
                    context=request.context,
                )
