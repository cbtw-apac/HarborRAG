from __future__ import annotations

import pytest

from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_engine.ingestion.indexing import (
    GenerationActivationPlan,
    GenerationActivationRequest,
    IndexGenerationActivationService,
)


class RecordingVectorRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def activate_generation(self, collection, **values) -> None:
        self.calls.append({"collection": collection, **values})


class RecordingGraphRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def activate_generation(self, **values) -> None:
        self.calls.append(values)


@pytest.mark.asyncio
async def test_activation_forwards_exact_deferred_mutations() -> None:
    vector = RecordingVectorRepository()
    graph = RecordingGraphRepository()
    context = StorageOperationContext(tenant_id="tenant-1")
    plan = GenerationActivationPlan(
        artifact_id="artifact-1",
        generation_id="generation-2",
        previous_generation_id="generation-1",
        vector_collection="documents",
        activate_vector_ids=("new",),
        retire_vector_ids=("changed",),
        delete_vector_ids=("removed",),
        tombstone_vector_ids=(),
    )

    await IndexGenerationActivationService(vector, graph).activate(
        GenerationActivationRequest(plan, context)
    )

    assert graph.calls == [
        {
            "artifact_id": "artifact-1",
            "generation_id": "generation-2",
            "previous_generation_id": "generation-1",
            "context": context,
        }
    ]
    assert vector.calls == [
        {
            "collection": "documents",
            "artifact_id": "artifact-1",
            "generation_id": "generation-2",
            "activate_ids": ("new",),
            "retire_ids": ("changed",),
            "delete_ids": ("removed",),
            "tombstone_ids": (),
            "context": context,
        }
    ]


def test_activation_plan_rejects_conflicting_vector_actions() -> None:
    with pytest.raises(ValueError, match="multiple activation actions"):
        GenerationActivationPlan(
            artifact_id="artifact-1",
            generation_id="generation-2",
            previous_generation_id="generation-1",
            vector_collection="documents",
            activate_vector_ids=("same",),
            retire_vector_ids=("same",),
            delete_vector_ids=(),
            tombstone_vector_ids=(),
        )
